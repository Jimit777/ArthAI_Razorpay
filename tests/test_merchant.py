"""
Tests for the merchant platform.

No agent calls. What is tested is the merchant journey, the gateway's
behaviour, and the properties that have to hold whatever anyone types into the
form - because the whole point of this platform is that the input is not a
fixture any more.

The one that matters most is the reconciliation invariant: whatever sequence of
sales, refunds and settlements happens, the bank credit must equal the sum of
the settlement lines. If that ever breaks, every number on the page is wrong.
"""

import sys
from pathlib import Path

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.detector import detect_batch  # noqa: E402
from engine.expected_value import load_rate_card  # noqa: E402
from merchant.gateway import (  # noqa: E402
    BEHAVIOUR_LABEL,
    BEHAVIOUR_NOTE,
    GATEWAY_RATES_BPS,
    Behaviour,
    capture,
    instrument_for,
)
from merchant.ledger import Ledger  # noqa: E402

RC = load_rate_card()


@pytest.fixture
def led(tmp_path):
    """A ledger already pointed at one business."""
    bootstrap = Ledger(tmp_path / "live.db")
    business_id = bootstrap.businesses.create("Test Boutique")
    bootstrap.close()
    ledger = Ledger(tmp_path / "live.db", business_id)
    yield ledger
    ledger.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A live app pointed at a throwaway database."""
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


# A fixed capture date, safely inside a month.
#
# Settlement is T+2 working days and the detector raises PERIOD_BOUNDARY when
# a sale and its settlement land in different months - correctly. Capturing at
# "now" therefore made these tests assert something about today's date: they
# were green for most of a month and went red in its last few days, and this
# suite flipped mid-afternoon on 28 August 2026 with no code change, because
# a sale that day settles on 1 September.
#
# A test that says "this is CLEAN" has to own the calendar, or it is testing
# the calendar.
SALE_DAY = int(datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc).timestamp())


def _sale(led, paise, method="upi", network=None, card_type=None, intl=False,
          when=SALE_DAY):
    order = led.create_order(paise, "test")
    return led.capture_payment(order, method, network, card_type, intl,
                               captured_at=when)


# --- the gateway does not know the answer -------------------------------

def test_the_gateway_never_imports_the_auditor():
    """
    The gateway and the auditor must meet only through the settlement file. If
    the gateway could see the merchant's rate card it would be deciding what to
    charge by reading the contract, which is not what gateways do - and a demo
    of a rate mismatch would be impossible to construct honestly.
    """
    import ast

    source = (Path(__file__).parent.parent / "merchant" / "gateway.py").read_text()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "engine" not in imported, "the gateway can see the merchant's rate card"
    assert "generator" not in imported

    # The docstring is allowed to MENTION the rate card - explaining why the
    # separation exists is the point. Only real imports are forbidden.
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "load_rate_card" not in called


def test_correct_behaviour_produces_no_findings(led):
    led.set_behaviour(Behaviour.CORRECT)
    for method, net, typ in [("upi", None, None), ("card", "visa", "credit"),
                             ("card", "rupay", "debit"), ("netbanking", None, None)]:
        _sale(led, 350_000, method, net, typ)

    batch = led.build_settlement(led.rate_card())
    for v in detect_batch(batch):
        assert v.exception_code == "CLEAN", f"{v.instrument_label}: {v.signals}"


@pytest.mark.parametrize("behaviour,method,network,card_type,expect", [
    (Behaviour.CARD_RATE_ON_UPI, "upi", None, None, "ZERO_MDR_RAIL_OVERCHARGED"),
    (Behaviour.CARD_RATE_ON_UPI, "card", "rupay", "debit", "ZERO_MDR_RAIL_OVERCHARGED"),
    (Behaviour.OVER_CONTRACT, "card", "visa", "credit", "RATE_ABOVE_CONTRACT"),
    (Behaviour.GST_ON_SALE_VALUE, "card", "visa", "credit",
     "GST_NOT_EIGHTEEN_PERCENT_OF_FEE"),
    (Behaviour.MISLABEL_UPI, "upi", None, None, "INSTRUMENT_MISLABEL_SIGNATURE"),
])
def test_each_misbehaviour_is_caught(led, behaviour, method, network, card_type, expect):
    """Every switch on the gateway panel has to actually produce a finding."""
    led.set_behaviour(behaviour)
    _sale(led, 470_000, method, network, card_type)
    variance = detect_batch(led.build_settlement(led.rate_card()))[0]
    assert expect in {s.kind for s in variance.signals}


def test_the_mislabel_leaves_no_arithmetic_gap(led):
    """
    The demo's sharpest moment, as a test. A UPI payment priced as a card is
    charged the CORRECT card fee - the numbers all match and it is still wrong.
    """
    led.set_behaviour(Behaviour.MISLABEL_UPI)
    _sale(led, 1_787_100, "upi")
    variance = detect_batch(led.build_settlement(led.rate_card()))[0]

    assert variance.fee_delta == 0
    assert variance.tax_delta == 0
    assert variance.raw["method"] == "card"
    assert variance.raw["upi_reference"], "the only trace left"
    assert variance.needs_agent


def test_every_behaviour_has_a_label_and_a_note():
    """The panel explains itself; an unlabelled switch is a magic trick."""
    for b in Behaviour:
        assert BEHAVIOUR_LABEL[b] and BEHAVIOUR_NOTE[b]


def test_gateway_money_is_always_integer_paise():
    for b in Behaviour:
        c = capture(162_733, "card", b, "visa", "credit")
        assert isinstance(c.fee, int) and isinstance(c.tax, int)
        assert c.fee >= 0 and c.tax >= 0


def test_the_debit_boundary_matches_the_rbi_threshold():
    assert instrument_for("card", "visa", "debit", False, 200_000) == "debit_card_low"
    assert instrument_for("card", "visa", "debit", False, 200_001) == "debit_card_high"


def test_every_instrument_the_gateway_can_pick_has_a_rate():
    for key in GATEWAY_RATES_BPS:
        assert key in RC["instruments"], f"{key} is not in the merchant rate card"


# --- the invariant everything else rests on -----------------------------

def test_the_bank_credit_always_equals_the_settlement_lines(led):
    """
    Whatever sequence of sales, refunds and misbehaviour happens, the money has
    to tie out. If this breaks, every figure on every page is wrong and the
    product's central claim - "the arithmetic is perfect and the rates are still
    wrong" - stops being true.
    """
    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    a = _sale(led, 162_700, "upi")
    _sale(led, 350_000, "card", "visa", "credit")
    b = _sale(led, 471_200, "card", "rupay", "debit")
    led.refund_payment(a)
    led.refund_payment(b)

    batch = led.build_settlement(led.rate_card())
    expected = sum(ln.amount - ln.fee - ln.tax
                   for r in batch.records for ln in r.settlement_lines)
    assert len(batch.bank_credits) == 1
    assert batch.bank_credits[0].amount == expected


def test_a_refunded_payment_keeps_its_fee(led):
    """Rule 8, produced by the platform rather than planted by the generator."""
    led.set_behaviour(Behaviour.CORRECT)
    pid = _sale(led, 350_000, "card", "visa", "credit")
    led.refund_payment(pid)

    batch = led.build_settlement(led.rate_card())
    lines = batch.records[0].settlement_lines
    refund_line = next(l for l in lines if l.type == "refund")
    payment_line = next(l for l in lines if l.type == "payment")
    assert refund_line.fee == 0 and refund_line.tax == 0
    assert payment_line.fee > 0

    variance = detect_batch(batch)[0]
    assert variance.exception_code == "REFUND_MDR_RETAINED"


# --- settlement mechanics -----------------------------------------------

def test_settling_twice_does_not_double_count(led):
    led.set_behaviour(Behaviour.CORRECT)
    _sale(led, 162_700, "upi")
    first = led.build_settlement(led.rate_card())
    led.commit_settlement(first)

    assert led.unsettled() == []
    assert led.build_settlement(led.rate_card()) is None, "nothing left to settle"


def test_a_settlement_can_be_reloaded_exactly(led):
    """
    Re-auditing a stored settlement after a rule changes is a normal thing to
    want. It has to produce identical inputs every time.
    """
    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    _sale(led, 162_700, "upi")
    _sale(led, 350_000, "card", "visa", "credit")
    batch = led.build_settlement(led.rate_card())
    run_id = led.commit_settlement(batch)

    reloaded = led.load_batch(run_id, RC)
    assert {r.record_id for r in reloaded.records} == \
           {r.record_id for r in batch.records}
    before = {v.payment_id: v.delta for v in detect_batch(batch)}
    after = {v.payment_id: v.delta for v in detect_batch(reloaded)}
    assert before == after


def test_behaviour_survives_a_restart(tmp_path):
    """Changing the gateway mid-demo must not be undone by a reload."""
    path = tmp_path / "persist.db"
    boot = Ledger(path)
    business_id = boot.businesses.create("Persist Co")
    boot.close()

    first = Ledger(path, business_id)
    first.set_behaviour(Behaviour.GST_ON_SALE_VALUE)
    first.close()

    again = Ledger(path, business_id)
    assert again.behaviour() == Behaviour.GST_ON_SALE_VALUE
    again.close()


def test_a_ledger_with_no_business_refuses_to_act(tmp_path):
    """
    The scoping is the only thing between two merchants' books in one file.
    An unscoped write must fail loudly rather than land somewhere plausible.
    """
    led = Ledger(tmp_path / "unscoped.db")
    with pytest.raises(ValueError, match="needs a business"):
        led.create_order(100_000, "whose order is this?")
    with pytest.raises(ValueError):
        led.unsettled()
    led.close()


def test_changing_behaviour_does_not_rewrite_history(led):
    """
    A payment keeps what was deducted at the time. Retroactively repricing old
    payments would be a gateway rewriting its own invoices.
    """
    led.set_behaviour(Behaviour.CORRECT)
    pid = _sale(led, 162_700, "upi")
    before = led.conn.execute(
        "SELECT fee, tax FROM live_payments WHERE payment_id = ?", (pid,)).fetchone()

    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    after = led.conn.execute(
        "SELECT fee, tax FROM live_payments WHERE payment_id = ?", (pid,)).fetchone()
    assert (after["fee"], after["tax"]) == (before["fee"], before["tax"])


# --- the web app ---------------------------------------------------------

def test_every_page_loads(client):
    _start(client)
    for path in ("/", "/settlements", "/agents", "/settings", "/about",
                 "/businesses", "/sources", "/simulator", "/ask"):
        assert client.get(path).status_code == 200, path


def _signup(client, email="owner@test.in", password="test-password", name="Owner"):
    """Create an account and stay signed in. First account becomes operator."""
    client.post("/signup", data={"email": email, "password": password,
                                 "name": name})


def _start(client, name="Test Shop", email=None):
    """Sign in, create a business, choose the simulator. Keeps the cookies."""
    if not client.cookies.get("session"):
        _signup(client, email or "owner@test.in")
    client.post("/businesses", data={"name": name})
    client.post("/sources/simulator")


def test_the_whole_journey_through_the_web_app(client):
    _start(client)
    client.post("/settings/gateway", data={"behaviour": "card_rate_on_upi"})
    client.post("/sale", data={"rupees": "1627.00", "description": "Scarf",
                               "instrument": "upi"})
    client.post("/sale", data={"rupees": "3500.00", "description": "Kurta",
                               "instrument": "visa_credit"})

    page = client.get("/simulator").text
    assert "Rs 1,627.00" in page and "Rs 3,500.00" in page

    settled = client.post("/settle", follow_redirects=False)
    assert settled.status_code == 303
    run_id = settled.headers["location"].rsplit("/", 1)[-1]

    detail = client.get(f"/settlements/{run_id}").text
    assert "Gross sales" in detail
    assert "reconciles to the paise" in detail
    assert "Not audited yet" in detail


def test_the_audit_runs_without_the_agent(client):
    """
    The rules alone must produce a usable audit. If the API is down at the
    venue, the calculator still works and the page still fills.
    """
    _start(client)
    client.post("/settings/gateway", data={"behaviour": "card_rate_on_upi"})
    client.post("/sale", data={"rupees": "1627.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]

    client.post(f"/audit/{run_id}", data={})       # no use_agent -> rules only
    import time
    for _ in range(50):
        if client.get(f"/audit/{run_id}/status").json()["state"] != "running":
            break
        time.sleep(0.1)

    status = client.get(f"/audit/{run_id}/status").json()
    assert status["state"] == "done", status
    page = client.get(f"/settlements/{run_id}").text
    assert "you can ask for back" in page
    assert "settled by your rate card alone" in page


def test_a_bad_amount_is_rejected(client):
    _start(client)
    assert client.post("/sale", data={"rupees": "not a number",
                                      "instrument": "upi"}).status_code == 400
    assert client.post("/sale", data={"rupees": "-5",
                                      "instrument": "upi"}).status_code == 400


def test_an_unknown_settlement_is_a_404(client):
    _start(client)
    assert client.get("/settlements/run_nonexistent").status_code == 404


def test_settling_nothing_is_harmless(client):
    """A stray click on an empty ledger must not produce an empty settlement."""
    _start(client)
    assert client.post("/settle", follow_redirects=False
                       ).headers["location"] == "/data/simulator"


def test_the_page_says_it_has_no_answer_key(client):
    """
    Honesty guard. A live settlement has no ground truth, so no accuracy number
    may appear next to it - otherwise the strongest claim in the project gets
    quietly attached to an anecdote.
    """
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    _pin_capture_dates()
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={})
    import time
    for _ in range(50):
        if client.get(f"/audit/{run_id}/status").json()["state"] != "running":
            break
        time.sleep(0.1)

    page = client.get(f"/settlements/{run_id}").text
    assert "no answer key" in page
    # The claim and a percentage must not sit together: this settlement has
    # nothing to be measured against, and a figure beside that sentence would
    # read as one.
    near = page.split("no answer key")[0][-400:]
    assert "%" not in near


def test_the_audit_survives_the_agent_being_unavailable(client, monkeypatch):
    """
    The venue has no wifi, or the credit has run out. The rules still work, and
    a rules-only audit in front of judges beats a blank page and an apology.
    """
    import agent.classifier as clf

    def _explode(*a, **kw):
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr(clf, "ClaudeClassifier", _explode)

    _start(client)
    client.post("/settings/gateway", data={"behaviour": "card_rate_on_upi"})
    client.post("/sale", data={"rupees": "1627.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={"use_agent": "yes"})

    import time
    for _ in range(50):
        if client.get(f"/audit/{run_id}/status").json()["state"] != "running":
            break
        time.sleep(0.1)

    status = client.get(f"/audit/{run_id}/status").json()
    assert status["state"] == "done", status
    assert "Agent unavailable" in status["note"]

    # and the page has to SAY so - a rules-only audit that looks like a full one
    # is worse than one that failed loudly
    page = client.get(f"/settlements/{run_id}").text
    assert "you can ask for back" in page
    assert "Agent unavailable" in page


def test_unticking_the_agent_box_actually_skips_the_agent(client):
    """
    An unchecked HTML checkbox submits nothing at all. A default of "yes" on the
    form field meant unticking the box still called the agent - which is the one
    setting whose entire purpose is to avoid needing a network or spending money.
    """
    import inspect

    import merchant.app as appmod

    default = inspect.signature(appmod.start_audit).parameters["use_agent"].default
    assert default.default == "no"


# --- multi-tenancy: the only thing between two merchants' books ----------

def test_two_businesses_cannot_see_each_others_payments(tmp_path):
    path = tmp_path / "two.db"
    boot = Ledger(path)
    a = boot.businesses.create("Meera's Boutique")
    b = boot.businesses.create("Ravi Electronics")
    boot.close()

    la, lb = Ledger(path, a), Ledger(path, b)
    la.capture_payment(la.create_order(162_700, "Scarf"), "upi")
    lb.capture_payment(lb.create_order(500_000, "Speaker"), "card", "visa", "credit")

    assert len(la.unsettled()) == 1
    assert len(lb.unsettled()) == 1
    assert {o["description"] for o in la.orders()} == {"Scarf"}
    assert {o["description"] for o in lb.orders()} == {"Speaker"}
    la.close(); lb.close()


def test_a_business_cannot_open_another_businesses_settlement(tmp_path):
    path = tmp_path / "runs.db"
    boot = Ledger(path)
    a = boot.businesses.create("A Co")
    b = boot.businesses.create("B Co")
    boot.close()

    la, lb = Ledger(path, a), Ledger(path, b)
    la.capture_payment(la.create_order(162_700, "x"), "upi")
    run_id = la.commit_settlement(la.build_settlement(la.rate_card()))

    assert la.owns_run(run_id)
    assert not lb.owns_run(run_id), "B can reach A's settlement by id"
    la.close(); lb.close()


def test_the_web_app_refuses_a_settlement_from_another_business(client):
    _start(client, "First Shop")
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]

    _start(client, "Second Shop")          # switches the cookie
    assert client.get(f"/settlements/{run_id}").status_code == 404
    assert client.post(f"/audit/{run_id}", data={}).status_code == 404


def test_each_business_negotiates_its_own_rates(tmp_path):
    """
    The reason the rate card had to move out of a shared file. "Charged above
    the contracted slab" is meaningless without a contract per merchant.
    """
    path = tmp_path / "rates.db"
    boot = Ledger(path)
    a = boot.businesses.create("Small Boutique")
    b = boot.businesses.create("Big Chain")
    # the chain negotiated a better credit-card rate
    boot.businesses.set_rate(b, "credit_card", 150, 0)
    boot.close()

    la, lb = Ledger(path, a), Ledger(path, b)
    assert la.rate_card()["instruments"]["credit_card"]["network_mdr_bps"] == 200
    assert lb.rate_card()["instruments"]["credit_card"]["network_mdr_bps"] == 150

    # and the same deduction is a finding for one and clean for the other
    from engine.detector import detect_batch
    for led in (la, lb):
        led.set_behaviour(Behaviour.CORRECT)
        led.capture_payment(led.create_order(1_000_000, "TV"), "card", "visa",
                            "credit", captured_at=SALE_DAY)

    # the gateway charges 2.00% to both; only the chain is overpaying
    a_var = detect_batch(la.build_settlement(la.rate_card()))[0]
    b_var = detect_batch(lb.build_settlement(lb.rate_card()))[0]
    assert a_var.exception_code == "CLEAN"
    assert b_var.fee_delta > 0, "the chain's better rate makes 2.00% an overcharge"
    la.close(); lb.close()


def test_a_regulated_rate_cannot_be_raised_by_contract(tmp_path):
    """
    The one rule a merchant does not get to edit is the one Parliament wrote.
    If someone could enter "UPI network MDR: 0.90%", the auditor would stop
    reporting zero-MDR violations - it would have been told they are contractual.
    """
    boot = Ledger(tmp_path / "reg.db")
    biz = boot.businesses.create("Tempted Co")

    for instrument in ("upi", "rupay_debit", "debit_card_low", "debit_card_high"):
        with pytest.raises(ValueError, match="capped"):
            boot.businesses.set_rate(biz, instrument, 900, 40)

    # negotiated slabs move freely
    boot.businesses.set_rate(biz, "credit_card", 175, 0)
    assert boot.businesses.rate_card(biz)["instruments"]["credit_card"][
        "network_mdr_bps"] == 175
    boot.close()


def test_the_rate_card_carries_the_guardrails_the_gate_needs(tmp_path):
    """
    Omitting these killed every audit with KeyError('guardrails'). The gate has
    no defaults on purpose - silently guessing what may be auto-closed is
    exactly the decision that should never be implicit.
    """
    boot = Ledger(tmp_path / "g.db")
    card = boot.businesses.rate_card(boot.businesses.create("Guard Co"))
    assert set(card["guardrails"]) == {"min_confidence", "review_above_paise"}
    assert set(card) >= {"gst_rate_bps", "tolerance", "instruments", "guardrails"}
    boot.close()


# --- the agent shelf -----------------------------------------------------

# The agents that actually have an implementation. Kept as a literal so that
# flipping a status to "live" without writing a runner fails here rather than
# in front of an audience.
IMPLEMENTED = {"settlement_audit", "gst_itc", "three_way_recon",
               "cash_forecaster"}


def test_the_live_agents_are_exactly_the_implemented_ones():
    """
    A convincing mock of a working reconciler is not a roadmap. This started as
    "exactly one agent is live" and failed loudly when the GST reconciler was
    implemented, which is what it was for - the list is updated deliberately,
    never widened to make a failure go away.
    """
    import merchant.agents.gst  # noqa: F401
    import merchant.agents.recon  # noqa: F401
    import merchant.agents.settlement  # noqa: F401
    import merchant.agents.treasury  # noqa: F401
    from merchant.catalog import all_agents, live_agents

    assert {a.id for a in live_agents()} == IMPLEMENTED
    assert len(all_agents()) > len(IMPLEMENTED), "the roadmap should be visible"


def test_a_live_agent_actually_has_something_to_run():
    import merchant.agents.gst  # noqa: F401
    import merchant.agents.recon  # noqa: F401
    import merchant.agents.settlement  # noqa: F401
    import merchant.agents.treasury  # noqa: F401
    from merchant.catalog import live_agents

    for spec in live_agents():
        assert spec.runner is not None, f"{spec.id} is live with no runner"
        assert spec.authority, f"{spec.id} argues from nothing"


def test_every_planned_agent_is_honestly_unrunnable():
    from merchant.catalog import all_agents

    for spec in all_agents():
        if spec.status == "planned":
            assert spec.runner is None
            assert not spec.is_live
            assert spec.why_unbuilt, f"{spec.id} does not say why the gap exists"


def test_a_planned_agent_cannot_be_turned_on(client):
    _start(client)
    response = client.post("/agents/payout_timing/toggle")
    assert response.status_code == 400
    assert "not built yet" in response.text


def test_the_live_agent_can_be_turned_off_and_on(client):
    _start(client)
    assert "Turn off" in client.get("/agents").text
    client.post("/agents/settlement_audit/toggle")
    page = client.get("/agents").text
    assert "Turn on" in page
    assert "turned off for this business" in client.get("/simulator").text


def test_agent_enablement_is_explicit_not_inferred(tmp_path):
    """
    Regression. Enablement used to be inferred from "no rows means everything
    is on", which held exactly until something was turned off: the row said
    enabled=0, the set came back empty, and it read as enabled again. A default
    you have to infer is a bug waiting for its second state.
    """
    boot = Ledger(tmp_path / "flags.db")
    biz = boot.businesses.create("Toggle Co")

    assert boot.businesses.agent_enabled(biz, "settlement_audit")
    boot.businesses.set_agent(biz, "settlement_audit", False)
    assert not boot.businesses.agent_enabled(biz, "settlement_audit")
    boot.businesses.set_agent(biz, "settlement_audit", True)
    assert boot.businesses.agent_enabled(biz, "settlement_audit")

    assert not boot.businesses.agent_enabled(biz, "payout_timing"), "never built"
    boot.close()


def test_every_page_shows_which_business_it_is_looking_at(client):
    """
    In a multi-tenant app with no login, the current tenant is the only context
    a person has. A page that does not show it invites acting on the wrong
    books - so the switcher is on every page, and it is not allowed to be
    pushed off-screen either.
    """
    _start(client, "Named Shop")
    for path in ("/", "/settlements", "/agents", "/settings", "/about",
                 "/sources", "/simulator", "/ask"):
        page = client.get(path).text
        assert "Named Shop" in page, path
        assert 'action="/switch"' in page, f"{path} has no business switcher"
        # and the data-source indicator, so nobody is ever wrong about whether
        # they are looking at a real merchant's money or manufactured data
        assert "demo data" in page or "razorpay" in page, path


# --- where the data comes from -------------------------------------------

def test_a_new_business_is_sent_to_choose_a_data_source(client):
    """
    The first question is not "record a sale", it is "where do your settlements
    come from". Getting that order wrong is what made a settlement auditor look
    like a point-of-sale system.
    """
    _signup(client)
    client.post("/businesses", data={"name": "Fresh Co"})
    assert client.get("/", follow_redirects=False).headers["location"] == "/data"

    page = client.get("/sources").text
    assert "Razorpay account" in page
    assert "Built-in simulator" in page
    assert "not where sales happen" in page


def test_live_razorpay_keys_are_refused(client):
    """
    No login exists. A credential that can reach real money must not be
    accepted, let alone stored.
    """
    _signup(client)
    client.post("/businesses", data={"name": "Tempted Co"})
    response = client.post("/sources/razorpay",
                           data={"key_id": "rzp_live_abcdef",
                                 "key_secret": "whatever"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert "test-mode+keys+only" in response.headers["location"].replace("%20", "+")


def test_the_plaintext_secret_never_reaches_the_database(tmp_path, monkeypatch):
    """
    The public key id is stored in the clear because it is public. The secret
    is stored encrypted or not at all - a grep of the raw file must never find
    it either way.
    """
    from merchant.sources import Sources, SourceKind
    from merchant.vault import ENV_KEY, Vault

    monkeypatch.setenv(ENV_KEY, Vault.generate_key())

    boot = Ledger(tmp_path / "src.db")
    biz = boot.businesses.create("Secret Co")
    sources = Sources(boot.conn)
    sources._set(biz, SourceKind.RAZORPAY, "rzp_test_public", "ok",
                 "Connected.", Vault.from_env().encrypt("the-actual-secret"))
    boot.close()

    blob = (tmp_path / "src.db").read_bytes()
    assert b"rzp_test_public" in blob, "the public id is stored in the clear"
    assert b"the-actual-secret" not in blob, "the secret leaked in plaintext"


def test_the_simulator_page_says_it_is_demo_data(client):
    _start(client)
    page = client.get("/simulator").text
    assert "Demo data" in page
    assert "stands in for a connected gateway" in page
    assert "/data" in page


def test_the_simulator_lives_under_data_rather_than_in_the_rail(client):
    """
    It used to appear and disappear from the sidebar depending on the data
    source, which meant the rail changed shape under you. It now sits under
    Data & integrations - where somebody looks for where the numbers come from
    - and the rail stays still.
    """
    _start(client)
    rail = client.get("/").text
    assert 'href="/data"' in rail
    assert client.get("/data/simulator").status_code == 200


def test_the_overview_is_not_a_form(client):
    """
    The front door shows settlements in, findings out - not a place to type
    sales into.
    """
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    page = client.get("/").text
    assert "recoverable from your gateway" in page
    assert "Needs your decision" in page
    assert "Take payment" not in page


def test_the_front_door_belongs_to_no_single_agent(client):
    """
    It used to carry an "Ask the auditor" box and a table of settlements -
    the settlement agent's dashboard wearing the name "Overview". With a
    second agent live that stopped being a simplification.
    """
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    page = client.get("/").text
    assert "Ask the auditor" not in page, "an agent-specific tool on the hub"
    assert 'action="/ask"' not in page
    # Both agents are represented, neither owns the page.
    assert "Settlement Deduction Auditor" in page
    assert "GST Input Credit Reconciler" in page


# --- the four business-process flows --------------------------------------

def test_every_flow_stage_names_a_real_agent():
    """A typo'd agent_id in nav.FLOWS would silently drop a stage rather than
    error - this is the test that would catch it."""
    import merchant.app  # noqa: F401  - importing this registers the four
                                        # live agents; without it catalog only
                                        # knows about the planned ones.
    from merchant import catalog, nav

    known = {spec.id for spec in catalog.all_agents()}
    for flow in nav.FLOWS:
        for stage in flow.stages:
            if stage.agent_id is not None:
                assert stage.agent_id in known, (
                    f"{flow.label}: {stage.label!r} names an unknown agent "
                    f"{stage.agent_id!r}")


def test_the_front_door_shows_all_four_categories_in_order(client):
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    page = client.get("/").text
    for label in ("Income Management", "Vendor Management",
                  "Treasury Management", "GST Management"):
        assert label in page

    order = [page.index(label) for label in
             ("Income Management", "Vendor Management",
              "Treasury Management", "GST Management")]
    assert order == sorted(order), "the categories are out of order"


def test_the_front_door_shows_plumbing_stages_honestly(client):
    """Sell and Pay are Razorpay's own plumbing - no agent claims them, and
    the card says so rather than looking like a silent failure."""
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    page = client.get("/").text
    assert "Nothing to audit yet" in page
    assert "Not a money-moving feature here" in page


def test_the_new_gst_filing_stage_is_planned_not_faked(client):
    """The fifth planned agent, added to fill the GST Management category -
    same honesty bar as the other four: greyed, a reason, no CTA."""
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    for page in (client.get("/").text, client.get("/agents").text):
        assert "GST Output Tax Reconciler" in page
        assert "Rule 88C" in page
        assert 'href="/agents/gst-filing"' not in page


def test_a_brand_new_business_is_told_what_to_do_next(client):
    """
    A screen of zeros is not neutral - it reads as broken, and it is the first
    impression every new business gets. Before there is anything to audit, the
    overview says what will appear here and gives exactly one next action.
    """
    _start(client, "Empty Co")
    page = client.get("/")

    assert page.status_code == 200
    text = page.text
    assert "Nothing has been settled yet" in text
    assert "What this screen will show" in text
    assert "Open the simulator" in text
    assert 'href="/data/simulator"' in text

    # and none of the zero-filled furniture
    assert "identified as recoverable" not in text
    assert "Needs your decision" not in text


def test_the_first_run_checklist_marks_what_is_already_done(client):
    _start(client, "Checklist Co")
    text = client.get("/").text
    assert "Business created" in text
    assert "Reading from Built-in simulator" in text
    assert "Run the auditor" in text
    assert 'class="step done"' in text
    assert 'class="step now"' in text
    assert 'class="step later"' in text


def test_a_razorpay_business_is_told_to_sync_not_to_type(client):
    """The next action depends on where the data is meant to come from."""
    _signup(client)
    client.post("/businesses", data={"name": "Connected Co"})

    from merchant.sources import SourceKind, Sources
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = [b for b in led.businesses.all()
               if b["name"] == "Connected Co"][0]["business_id"]
        Sources(led.conn)._set(biz, SourceKind.RAZORPAY, "rzp_test_x", "ok", "ok")

    text = client.get("/").text
    assert "Pull your settlements" in text
    assert "Open the simulator" not in text


def test_empty_tables_offer_the_next_step(client):
    """"No settlements yet" with no way forward is a dead end, not an empty state."""
    _start(client)
    settlements = client.get("/settlements").text
    assert "No settlements yet" in settlements
    assert "Take a payment" in settlements
    assert 'href="/data/simulator"' in settlements

    simulator = client.get("/simulator").text
    assert "No sales yet" in simulator
    assert "the auditor checks whether that fee was right" in simulator


def test_every_behaviour_declares_which_rails_it_touches(led):
    """
    The picker tells the truth about what each fault does, and the truth is
    checked here rather than written once and left to drift. Set "card rate on
    UPI", take a Visa credit payment, and the auditor correctly finds nothing -
    which looks like a broken auditor unless the picker said so first.
    """
    from merchant.gateway import BEHAVIOUR_AFFECTS, BEHAVIOUR_FINDS

    instruments = {
        "UPI": ("upi", None, None),
        "RuPay debit": ("card", "rupay", "debit"),
        "Visa/Mastercard debit": ("card", "visa", "debit"),
        "Visa/Mastercard credit": ("card", "visa", "credit"),
        "Amex": ("card", "amex", "credit"),
        "Netbanking": ("netbanking", None, None),
        "Wallet": ("wallet", None, None),
    }

    for behaviour in Behaviour:
        claimed = BEHAVIOUR_AFFECTS[behaviour]
        universal = claimed == ["every instrument"]
        for label, (method, network, card_type) in instruments.items():
            led.set_behaviour(behaviour)
            _sale(led, 500_000, method, network, card_type)
            variance = detect_batch(led.build_settlement(led.rate_card()))[0]
            found = variance.exception_code != "CLEAN" or bool(variance.signals)
            should = universal or label in claimed
            assert found == should, (
                f"{behaviour.value} on {label}: picker claims "
                f"{'a finding' if should else 'clean'}, auditor said "
                f"{'a finding' if found else 'clean'}")
            # settle it away so the next pass starts clean
            led.commit_settlement(led.build_settlement(led.rate_card()))

    assert set(BEHAVIOUR_FINDS) == set(Behaviour)


def test_the_settings_page_shows_a_worked_example_per_rate(client):
    """
    "0.40%" is abstract. "Rs 4.00 + Rs 0.72 GST on a Rs 1,000 sale" is not.
    The auditor argues in rupees, so the page a merchant checks their contract
    against should too.
    """
    _start(client)
    page = client.get("/settings").text
    assert "On a Rs 1,000.00 sale" in page
    assert "GST" in page


def test_the_fault_switch_lives_with_the_simulator_not_in_settings(client):
    """
    No real merchant has a "make my gateway misbehave" control. It is a demo
    instrument, and a customer-facing settings page is the wrong place for it -
    it was the single most confusing thing in the app.
    """
    _start(client)
    settings = client.get("/settings").text
    simulator = client.get("/simulator").text

    assert "How the gateway behaves" in simulator
    assert "How the gateway behaves" not in settings
    assert "Gateway simulator" not in settings
    assert 'action="/settings/gateway"' not in settings


def test_the_fault_picker_says_which_rails_it_touches(client):
    """
    The confusion this fixes: set "card rate on UPI", take a Visa credit
    payment, find nothing, and conclude the auditor is broken. It is not - the
    fault does not apply to that rail, and the picker says so first.
    """
    _start(client)
    page = client.get("/simulator").text
    assert "Applies to UPI, RuPay debit." in page
    assert "there is nothing there to find" in page
    assert "finds ZERO_MDR_VIOLATION" in page
    assert "finds nothing - a clean sheet" in page


def test_settings_is_only_the_merchants_own_contract(client):
    """
    What is left on Settings belongs to the merchant and nobody else: their
    rate card, their tax and tolerance, their review thresholds. No platform
    controls, no demo instruments.
    """
    _start(client)
    page = client.get("/settings").text
    assert "Your rate card" in page
    assert "Tax and tolerance" in page
    assert "What the agent may close by itself" in page
    for stranger in ("Gateway simulator", "Agent rollout", "Accounts"):
        assert stranger not in page


def test_guardrail_thresholds_can_be_changed(client):
    _start(client)
    client.post("/settings/guardrails",
                data={"min_confidence": "0.9", "review_above": "1000"})

    import merchant.app as appmod
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        card = led.businesses.rate_card(biz)
    assert card["guardrails"]["min_confidence"] == 0.9
    assert card["guardrails"]["review_above_paise"] == 100_000


def test_an_impossible_confidence_threshold_is_refused(client):
    _start(client)
    response = client.post("/settings/guardrails",
                           data={"min_confidence": "1.8", "review_above": "250"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_saving_a_rate_confirms_what_it_became(client):
    """Silence after a save is indistinguishable from a save that did nothing."""
    _start(client)
    response = client.post("/settings/rate",
                           data={"instrument": "credit_card",
                                 "network_pct": "1.75", "platform_pct": "0"},
                           follow_redirects=False)
    assert "ok=" in response.headers["location"]
    assert "1.75" in response.headers["location"]


def test_raising_a_regulated_rate_is_refused_with_a_reason(client):
    _start(client)
    response = client.post("/settings/rate",
                           data={"instrument": "upi", "network_pct": "0.90",
                                 "platform_pct": "0.40"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert "capped" in response.headers["location"]


# --- what a person sees during the fifteen seconds ----------------------

def test_the_rules_result_is_reported_before_the_agent_starts(led):
    """
    The interesting half of the work finishes in milliseconds. A merchant
    watching a progress bar should see that most of their settlement was
    resolved by arithmetic and never went near a language model - not wait
    twenty seconds to be told so.
    """
    from merchant.agents.settlement import run_settlement_audit
    from merchant.catalog import AgentContext

    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    _sale(led, 162_700, "upi")
    _sale(led, 350_000, "card", "visa", "credit")
    run_id = led.commit_settlement(led.build_settlement(led.rate_card()))

    seen = []
    run_settlement_audit(AgentContext(
        business_id=led.business_id, rate_card=led.rate_card(),
        db=str(led.store.path), target_id=run_id, use_agent=False,
        progress=lambda **kw: seen.append(kw)))

    rules = next(u for u in seen if "settled_by_rules" in u)
    assert rules["settled_by_rules"] >= 1
    assert rules["rules_breakdown"], "the breakdown by code is reported"
    assert "settled by the rate card alone" in rules["phase"]

    # and it arrives before anything about the agent
    assert seen.index(rules) < len(seen) - 1


def test_each_verdict_streams_as_it_lands(led):
    """
    Twenty seconds of nothing followed by everything at once tells a watcher
    less than the same information arriving as it is decided.
    """
    from agent.classifier import Verdict
    from merchant.agents.settlement import run_settlement_audit
    from merchant.catalog import AgentContext

    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    _sale(led, 162_700, "upi")
    _sale(led, 471_200, "card", "rupay", "debit")
    run_id = led.commit_settlement(led.build_settlement(led.rate_card()))

    class _Fake:
        def __init__(self, batch, memory=None):
            pass

        def classify(self, variance, on_event=None):
            # the real classifier reports these DURING the call
            if on_event:
                on_event("weighing")
                on_event("tool", "rate_card_lookup")
            return Verdict(payment_id=variance.payment_id,
                           exception_code="ZERO_MDR_VIOLATION", action="dispute",
                           confidence=0.95, reasoning="x", rule_cited="rule 1")

    import agent.classifier as clf
    original = clf.ClaudeClassifier
    clf.ClaudeClassifier = _Fake
    try:
        seen = []
        run_settlement_audit(AgentContext(
            business_id=led.business_id, rate_card=led.rate_card(),
            db=str(led.store.path), target_id=run_id, use_agent=True,
            progress=lambda **kw: seen.append(kw)))
    finally:
        clf.ClaudeClassifier = original

    results = [u["result"] for u in seen if "result" in u]
    assert len(results) == 2
    for r in results:
        assert r["payment_id"] and r["instrument"]
        assert r["code"] == "ZERO_MDR_VIOLATION"
        assert r["stake"].startswith("Rs ")
        assert r["confidence"] == 0.95


def test_streamed_results_accumulate_rather_than_overwrite(client):
    """
    The progress sink treats `result` specially. If it merged like every other
    field, each verdict would replace the last and the page would show one row
    however many records were audited.
    """
    import merchant.app as appmod

    update = appmod._progress("run_test")
    update(phase="working", total=3)
    update(result={"payment_id": "pay_a", "code": "CLEAN"})
    update(result={"payment_id": "pay_b", "code": "RATE_MISMATCH"})
    update(done=2)

    state = appmod.RUNS["run_test"]
    assert [r["payment_id"] for r in state["results"]] == ["pay_a", "pay_b"]
    assert state["done"] == 2 and state["phase"] == "working"
    assert "result" not in state, "the singular key must not linger"


def test_the_live_view_is_rendered_while_an_audit_runs(client):
    _start(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]

    import merchant.app as appmod
    appmod.RUNS[run_id] = {"state": "running", "phase": "Starting", "done": 0,
                           "total": 2, "settled_by_rules": 5, "current": "",
                           "note": "", "agent": "Settlement Deduction Auditor",
                           "started": 0}

    page = client.get(f"/settlements/{run_id}").text
    # the live view is the terminal, streaming - not a separate widget
    assert 'class="agentterm"' in page
    assert "RUNNING" in page
    assert "at-cursor" in page
    assert "Everything it thinks and does, as it happens." in page
    appmod.RUNS.pop(run_id, None)


def test_the_palette_does_not_follow_the_viewers_os_setting(client):
    """
    A finance dashboard gets shown on other people's machines and on
    projectors. A palette that flips with the viewer's dark-mode setting means
    nobody can be sure what it looks like when it matters - and it did flip,
    in Chrome, unprompted.
    """
    from merchant.views import CSS

    assert "prefers-color-scheme" not in CSS

    # and the part that is easy to miss: without color-scheme, a browser in
    # dark mode renders native inputs, selects and scrollbars dark anyway
    assert "color-scheme: light" in CSS

    _start(client)
    page = client.get("/").text
    assert "color-scheme: light" in page


def test_the_terminal_stays_dark_regardless(client):
    """A terminal that turns white stops reading as a transcript."""
    from merchant.views import CSS

    assert ".agentterm { border-radius:10px; overflow:hidden; background:#0f1319" in CSS


# --- a law changing must not need a hand-edit per business ---------------

def test_a_regulated_citation_follows_the_law_not_the_stored_copy(tmp_path):
    """
    The authority under rules 1 and 2 changed on 17 Aug 2026. Every business
    created before that date had the old citation copied into its own rate-card
    rows, so reading the citation from those rows would have meant hand-editing
    every business on the platform - and any that were missed would go on
    citing a link that no longer exists.

    A merchant cannot negotiate a regulated rate, so the authority for it is
    not theirs to hold a copy of. It is read from the reference card.
    """
    from merchant.businesses import Businesses, REGULATED, reference_rate_card

    led = Ledger(str(tmp_path / "m.db"))
    bid = led.businesses.create("Old Shop")

    # a business whose stored citation predates the change
    stale = "PSS Act s.10A read with IT Act s.269SU, effective 2020-01-01"
    led.conn.execute(
        "UPDATE business_rate_card SET network_mdr_source = ?"
        " WHERE business_id = ? AND instrument = 'upi'", (stale, bid))
    led.conn.commit()

    card = led.businesses.rate_card(bid)
    current = reference_rate_card()["instruments"]["upi"]["network_mdr_source"]
    assert card["instruments"]["upi"]["network_mdr_source"] == current
    assert "269SU" not in card["instruments"]["upi"]["network_mdr_source"]


def test_a_negotiated_citation_is_still_the_merchants_own(tmp_path):
    """
    Only regulated rows are overridden. What a merchant negotiated with their
    gateway is theirs, and the reference card has no business overwriting it.
    """
    led = Ledger(str(tmp_path / "m.db"))
    bid = led.businesses.create("Shop")
    led.conn.execute(
        "UPDATE business_rate_card SET platform_fee_source = ?"
        " WHERE business_id = ? AND instrument = 'upi'",
        ("our 2026 contract, clause 4", bid))
    led.conn.commit()

    card = led.businesses.rate_card(bid)
    assert card["instruments"]["upi"]["platform_fee_source"] == \
        "our 2026 contract, clause 4"


def test_the_zero_mdr_citation_the_engine_prints_matches_the_rate_card(tmp_path):
    """The finding and the contract it argues from must not drift apart."""
    from engine.detector import SOURCE_ZERO_MDR
    from merchant.businesses import reference_rate_card

    reference = reference_rate_card()["instruments"]
    assert reference["upi"]["network_mdr_source"] == SOURCE_ZERO_MDR
    assert reference["rupay_debit"]["network_mdr_source"] == SOURCE_ZERO_MDR


# --- the information architecture ----------------------------------------

# Everything in the rail that is not an agent: Home, Agents, Data, Businesses,
# Business settings, Team, Activity, Admin, Accuracy.
FIXED_RAIL_ITEMS = 9


def test_the_sidebar_stays_small_as_agents_are_added(client):
    """
    Every new agent used to add three root-level items. The rail is now driven
    by merchant/nav.py, where an agent contributes ONE entry under Workspace
    and everything else becomes a tab inside its own workspace.

    Asserted as that invariant rather than as a maximum. The maximum was a
    number that had to be raised every time a real agent shipped, which meant
    the test failed for the one reason it was not about - and a tripwire that
    cries wolf on good news is one people learn to bump without reading.
    """
    from merchant.catalog import live_agents

    _start(client)
    rail = client.get("/").text
    rail = rail.split('<nav>')[1].split('</nav>')[0]

    items = rail.count('class="item')
    allowed = FIXED_RAIL_ITEMS + len(live_agents())
    assert items <= allowed, (
        f"the rail has {items} items for {len(live_agents())} agents - "
        f"an agent is contributing more than one entry again")


def test_agent_pages_live_under_their_agent(client):
    _start(client)
    for path in ("/agents/settlement", "/agents/settlement/ask",
                 "/agents/settlement/setup", "/agents/input-credit",
                 "/agents/input-credit/purchases",
                 "/agents/input-credit/setup"):
        assert client.get(path).status_code == 200, path


def test_every_moved_url_still_works(client):
    """
    Bookmarks and muscle memory point at the old paths. A refactor that breaks
    them is one nobody can verify.
    """
    from merchant.nav import MOVED

    _start(client)
    for old, new in MOVED.items():
        if "{" in old:
            continue
        response = client.get(old, follow_redirects=False)
        assert response.status_code == 307, old
        assert response.headers["location"] == new, old
        assert client.get(old).status_code == 200, old


def test_a_workspace_says_whether_it_is_on_live_or_demo_data(client):
    _start(client)
    page = client.get("/agents/settlement").text
    assert "Demo data" in page


def test_the_setup_tab_folded_into_the_tab_it_configures(client):
    """
    Setup is gone. The only thing on it this agent needed was the GSP
    connection, and that belongs beside the flow it enables rather than one
    tab away from it - so the old URL lands there.
    """
    _start(client)
    moved = client.get("/agents/input-credit/setup", follow_redirects=False)
    assert moved.status_code == 307
    assert moved.headers["location"] == "/agents/input-credit/with-api"

    page = client.get("/agents/input-credit/with-api").text
    assert "Connect a GST filing-status API" in page


def test_the_hub_shows_planned_agents_without_pretending(client):
    _start(client)
    page = client.get("/agents").text
    assert "Coming soon" in page
    assert "Income Management" in page
    assert "cannot be switched on for anyone" in page


def test_the_hub_carries_the_toggle_home_does_not(client):
    """The control belongs on the hub, a settings-flavoured screen, not on
    Home, which is a summary - same distinction the page's own docstring
    draws."""
    _start(client)
    hub = client.get("/agents").text
    home = client.get("/").text
    assert 'action="/agents/settlement_audit/toggle"' in hub
    assert 'action="/agents/settlement_audit/toggle"' not in home


# --- the settlement page reads like a decision, not a log ----------------

def _pin_capture_dates():
    """
    Push every captured payment back to SALE_DAY.

    /sale captures at "now" and settlement is T+2 working days, so on the last
    few days of a month these sales settle into the next one - which raises a
    correct PERIOD_BOUNDARY finding and quietly changes what every page-level
    assertion sees. Tests about a page should not also be testing the
    calendar.
    """
    import merchant.app as appmod

    with appmod.ledger(None) as led:
        led.conn.execute("UPDATE live_payments SET captured_at = ?", (SALE_DAY,))
        led.conn.execute("UPDATE live_orders SET created_at = ?", (SALE_DAY,))
        led.conn.commit()


def _audited(client, behaviour="card_rate_on_upi"):
    """
    Two sales, settled and audited, through the real routes.

    The capture dates are pushed back to SALE_DAY afterwards, because /sale
    captures at "now" and settlement is T+2 working days - so on the last few
    days of a month these sales settle into the next one and every assertion
    about a CLEAN page fails on a calendar technicality. These tests are about
    the page, not about today's date.
    """
    import time

    _start(client)
    client.post("/settings/gateway", data={"behaviour": behaviour})
    client.post("/sale", data={"rupees": "9000.00", "instrument": "upi"})
    client.post("/sale", data={"rupees": "4500.00", "instrument": "visa_credit"})

    _pin_capture_dates()

    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={})
    for _ in range(80):
        if client.get(f"/audit/{run_id}/status").json()["state"] != "running":
            break
        time.sleep(0.1)
    return run_id


def test_the_settlement_page_shows_what_the_agent_did(client):
    """
    The trace was folded away in a redesign and read as deleted. It is the
    demo: a merchant may want the decision first, but the person showing this
    to a room needs the agent's working visible without a click.
    """
    run_id = _audited(client)
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert "What the agent did" in page
    assert 'id="rp-go"' in page, "the replay control"
    assert "at-body" in page, "the terminal itself"


def test_the_money_still_comes_first(client):
    """Above the trace, because it is the question a merchant arrived with."""
    run_id = _audited(client)
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert (page.index("What happened to the money")
            < page.index("What the agent did"))


def test_the_settle_and_audit_controls_are_reachable(client):
    """
    Take a payment, settle it, audit it - the loop the whole product is. Each
    step has to be a visible control on the page a person is already on.
    """
    _start(client)
    assert 'action="/sale"' in client.get("/data/simulator").text

    client.post("/sale", data={"rupees": "9000.00", "instrument": "upi"})
    # Settle appears once there is something to settle, which is why the order
    # of these two assertions matters.
    assert 'action="/settle"' in client.get("/data/simulator").text
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert f'action="/audit/{run_id}"' in page


def test_every_code_has_words_a_merchant_would_use():
    """
    ZERO_MDR_VIOLATION is our enum. The one word in it a merchant recognises -
    "violation" - is the one that tells them least about what to do. Checked
    over the whole taxonomy so a new code cannot ship without a translation.
    """
    from engine.taxonomy import ExceptionCode
    from merchant.app import SETTLEMENT_EXPLAIN, SETTLEMENT_ISSUE

    for code in ExceptionCode:
        assert str(code) in SETTLEMENT_ISSUE, f"{code} has no plain name"
        assert str(code) in SETTLEMENT_EXPLAIN, f"{code} has no explanation"
        assert "_" not in SETTLEMENT_ISSUE[str(code)]


def test_the_page_shows_the_words_and_keeps_the_code_for_support(client):
    run_id = _audited(client)
    page = client.get(f"/agents/settlement/run/{run_id}").text
    # The agent is off in this fixture, so these escalate rather than classify.
    assert "Needs a person" in page
    assert 'title="UNEXPLAINED"' in page


# --- resolution memory, end to end ----------------------------------------
#
# CLAUDE.md section 12. The store-level round trip is tested in test_store.py;
# what matters here is that a human clicking something in the actual app is
# the only way any of it gets written, and that it stays inside one business.

def test_resolving_a_finding_marks_it_reviewed_and_remembers_it(client):
    """
    _audited's two sales produce two variance rows - one CLEAN (the card
    sale, untouched by the planted behaviour) and one exception (the UPI
    sale). Without the exception_code filter this was flaky: an unordered
    SELECT can return either row, and resolving the CLEAN one - which the
    page never renders a card for - left "Reviewed" nowhere to be found.
    """
    import merchant.app as appmod

    run_id = _audited(client)
    with appmod.ledger(None) as led:
        biz = led.businesses.all()[0]["business_id"]
        found = led.conn.execute(
            "SELECT payment_id, exception_code, human_reviewed FROM variances"
            " WHERE run_id = ? AND exception_code NOT IN ('CLEAN', 'ROUNDING')",
            (run_id,)).fetchone()
    assert found is not None, "the planted behaviour produced no exception"
    assert found["human_reviewed"] == 0

    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert 'action="/agents/settlement/resolve"' in page
    assert "Mark this resolved" in page

    resp = client.post("/agents/settlement/resolve", data={
        "run_id": run_id, "payment_id": found["payment_id"],
        "exception_code": found["exception_code"],
        "resolution": "Disputed with Razorpay, ticket #4471"},
        follow_redirects=False)
    assert resp.status_code == 303
    assert "Saved" in resp.headers["location"]

    with appmod.ledger(None) as led:
        row = led.conn.execute(
            "SELECT human_reviewed FROM variances WHERE run_id = ? AND"
            " payment_id = ?", (run_id, found["payment_id"])).fetchone()
        assert row["human_reviewed"] == 1

        remembered = led.store.resolutions(
            found["exception_code"], business_id=biz)
        assert len(remembered) == 1
        assert remembered[0]["payment_id"] == found["payment_id"]
        assert "ticket #4471" in remembered[0]["resolution"]

    after = client.get(f"/agents/settlement/run/{run_id}").text
    assert "Reviewed" in after
    assert "Mark this resolved" not in after


def test_resolving_without_a_reason_is_refused(client):
    """A blank note is not a resolution - there is nothing for the agent to
    recall from it."""
    import merchant.app as appmod

    run_id = _audited(client)
    with appmod.ledger(None) as led:
        found = led.conn.execute(
            "SELECT payment_id, exception_code FROM variances"
            " WHERE run_id = ? AND exception_code NOT IN ('CLEAN', 'ROUNDING')",
            (run_id,)).fetchone()
    assert found is not None, "the planted behaviour produced no exception"

    resp = client.post("/agents/settlement/resolve", data={
        "run_id": run_id, "payment_id": found["payment_id"],
        "exception_code": found["exception_code"], "resolution": "   "},
        follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    with appmod.ledger(None) as led:
        row = led.conn.execute(
            "SELECT human_reviewed FROM variances WHERE run_id = ? AND"
            " payment_id = ?", (run_id, found["payment_id"])).fetchone()
        assert row["human_reviewed"] == 0
        assert led.store.resolutions(found["exception_code"]) == []


def test_a_business_cannot_resolve_another_businesss_finding(client):
    run_id = _audited(client)

    _start(client, "Second Shop")          # switches the cookie
    resp = client.post("/agents/settlement/resolve", data={
        "run_id": run_id, "payment_id": "pay_whatever",
        "exception_code": "UNEXPLAINED", "resolution": "not yours"})
    assert resp.status_code == 404


def test_every_finding_recommends_something(client):
    run_id = _audited(client)
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert "Recommended" in page
    assert "Show the working" in page



def test_the_disclaimer_is_a_banner(client):
    run_id = _audited(client)
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert 'class="banner brand"' in page
    assert "has been disputed or written off" in page


def test_clean_payments_are_counted_not_listed(client):
    """
    Five cards, four of which say "nothing is wrong", is four cards of noise.
    The count carries them.
    """
    run_id = _audited(client, behaviour="correct")
    page = client.get(f"/agents/settlement/run/{run_id}").text
    assert "Nothing was charged wrongly" in page
    assert "Needs review" not in page


def test_a_settlement_follows_its_payments_not_the_clock(tmp_path):
    """
    Regression, and it took the whole suite red mid-afternoon.

    settled_at was `now + T+2 working days` rather than T+2 from the last
    payment in the batch. In the simulator those are minutes apart, so it
    never showed - until 28 August 2026, when a same-day sale settled on
    1 September, the detector correctly raised PERIOD_BOUNDARY on every
    otherwise-clean payment, and eight tests that had passed that morning
    failed that afternoon with no code change.

    A settlement follows the payments it settles. It does not follow the
    moment somebody pressed the button.
    """
    from datetime import datetime, timezone

    from engine.expected_value import SETTLEMENT_WORKING_DAYS, add_working_days

    led = Ledger(tmp_path / "t.db")
    led.business_id = led.businesses.create("Meera's Boutique")
    led.set_behaviour(Behaviour.CORRECT)

    captured = int(datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc).timestamp())
    led.capture_payment(led.create_order(350_000, "Scarf"), "upi",
                        captured_at=captured)

    batch = led.build_settlement(led.rate_card())
    expected = int(add_working_days(
        datetime.fromtimestamp(captured, timezone.utc),
        SETTLEMENT_WORKING_DAYS).timestamp())
    assert batch.records[0].settlement_lines[0].settled_at == expected

    # And a June sale settling in June is clean, whatever today happens to be.
    from engine.detector import detect_batch

    assert detect_batch(batch)[0].exception_code == "CLEAN"
    led.close()


# --- the home queue, every live agent --------------------------------------
#
# _open_decisions used to read from two of the four live agents. Cash forecast
# and three-way recon results live in a run-state dict rather than the
# database, so the home queue - and the agent cards above it - silently
# treated them as if they had never been run. See _latest_cash_run,
# _latest_recon_run in merchant/app.py.

def _forecast(client, source="demo"):
    """Run a demo cash forecast with no agent, wait for it, return its key."""
    import time as _time

    key = client.post("/agents/cash-forecaster/run", data={"source": source},
                      follow_redirects=False).headers["location"].split("key=")[-1]
    for _ in range(80):
        r = client.get(f"/agents/cash-forecaster/{key}.json")
        if r.json().get("state") != "running":
            break
        _time.sleep(0.1)
    return key


def _reconciled(client, source="demo"):
    """Run a demo three-way reconciliation with no agent, wait, return its key."""
    import time as _time

    key = client.post("/agents/three-way/run", data={"source": source},
                      follow_redirects=False).headers["location"].split("key=")[-1]
    for _ in range(80):
        r = client.get(f"/agents/three-way/{key}.json")
        if r.json().get("state") != "running":
            break
        _time.sleep(0.1)
    return key


def test_a_forecast_that_needs_a_decision_reaches_the_home_queue(client):
    """
    The planted demo scenario is built to need a decision (CASH_CRUNCH_WARNING,
    coverable by delaying a payout) - CLAUDE.md's own ground truth. If this
    never reaches the queue, the queue's claim to cover every agent is false.
    """
    _start(client)
    _forecast(client)

    page = client.get("/").text
    assert "Cash forecast" in page
    assert "Needs your decision" in page


def test_a_healthy_forecast_does_not_clutter_the_queue(client):
    """ACT_NONE and ACT_WATCH mean nothing to decide - same taxonomy discipline
    as every other agent here: most of the codes mean 'do nothing', and the
    queue has to honour that or it trains people to ignore it."""
    from engine.treasury.records import ACT_NONE

    import merchant.app as appmod

    _start(client)
    key = _forecast(client)
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"]["forecast"]["action"] = ACT_NONE

    assert "Cash forecast" not in client.get("/").text


def test_a_reconciliation_with_exceptions_reaches_the_home_queue(client):
    _start(client)
    _reconciled(client)

    page = client.get("/").text
    assert "Three-way recon" in page
    assert "Needs your decision" in page


def test_the_agent_cards_stop_lying_about_setup_needed(client):
    """
    Same bug, one layer up: _agent_state fell through to STATE_SETUP for these
    two agents regardless of whether they had ever run, so the card said "Set
    it up" for an agent that had already produced a real recommendation.
    """
    _start(client)
    _forecast(client)
    _reconciled(client)

    page = client.get("/").text
    assert "auto-reconciled" in page
    assert "shortfall at the low point" in page


def test_an_unrun_agent_still_says_set_it_up(client):
    """No regression for the common case: a business that has never touched
    these two agents should see exactly what it saw before."""
    _start(client, "Untouched Co")
    page = client.get("/").text
    assert "shortfall at the low point" not in page
    assert "auto-reconciled" not in page


def test_a_business_cannot_see_another_businesss_forecast_in_the_queue(client):
    _start(client, "First Shop")
    _forecast(client)

    _start(client, "Second Shop")          # switches the cookie
    page = client.get("/").text
    assert "Cash forecast" not in page


# --- gateway_fee_credit: GST paid to Razorpay, surfaced as claimable -------
#
# The fourth cross-agent connection, and a different shape from the other
# three: not an agent asking another agent's findings about the same record,
# but a fact the settlement auditor already verified that the GST reconciler's
# purchase register has never heard of. See Ledger.gateway_fee_credit().

def _plant_variance(led, business_id, payment_id, run_id="run_1", **overrides):
    row = {
        "exception_code": "CLEAN", "actual_tax": 1800, "created_at": 0,
    }
    row.update(overrides)
    led.conn.execute(
        "INSERT OR IGNORE INTO business_runs (run_id, business_id, created_at)"
        " VALUES (?,?,0)", (run_id, business_id))
    led.conn.execute(
        "INSERT INTO variances (payment_id, run_id, expected_fee, actual_fee,"
        " expected_tax, actual_tax, delta, money_at_stake, exception_code,"
        " confidence, reasoning, action, human_reviewed, queued_for_human,"
        " created_at) VALUES (?,?,0,0,0,?,0,0,?,1.0,'ok','dismiss',0,0,?)",
        (payment_id, run_id, row["actual_tax"], row["exception_code"],
         row["created_at"]))
    led.conn.commit()


def test_gateway_fee_credit_sums_clean_and_rounding_tax(led):
    _plant_variance(led, led.business_id, "pay_1", exception_code="CLEAN",
                    actual_tax=1800)
    _plant_variance(led, led.business_id, "pay_2", exception_code="ROUNDING",
                    actual_tax=3600)

    result = led.gateway_fee_credit()

    assert result["paise"] == 5400
    assert result["count"] == 2


def test_gateway_fee_credit_excludes_disputed_fees(led):
    """A fee under dispute carries a disputed tax figure on top of it -
    claiming credit on a number that might still change is exactly the
    mistake claiming credit exists to avoid."""
    _plant_variance(led, led.business_id, "pay_1", exception_code="CLEAN",
                    actual_tax=1800)
    _plant_variance(led, led.business_id, "pay_2",
                    exception_code="ZERO_MDR_VIOLATION", actual_tax=9999)

    result = led.gateway_fee_credit()

    assert result["paise"] == 1800
    assert result["count"] == 1


def test_gateway_fee_credit_with_no_settlements_is_zero(led):
    result = led.gateway_fee_credit()
    assert result["paise"] == 0
    assert result["count"] == 0


def test_gateway_fee_credit_is_scoped_to_one_business(led, tmp_path):
    other_id = led.businesses.create("Someone Else")
    _plant_variance(led, other_id, "pay_other", run_id="run_other",
                    actual_tax=50000)

    result = led.gateway_fee_credit()

    assert result["paise"] == 0, (
        "another business's gateway fee GST leaked through")
