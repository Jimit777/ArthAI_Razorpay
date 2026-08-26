"""
Tests for the supplier watch as a deployed feature.

The engine tests prove it notices the right things. These prove it remembers -
a watch with no memory of last time is a reconciliation on a timer, which is
the thing it exists not to be.
"""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst.watch import STATUS_UNKNOWN  # noqa: E402

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


def _buy(client, supplier, rupees="60000", behaviour="correct"):
    """Through the ledger - the manual purchase screen is gone."""
    import merchant.app as appmod
    from merchant.suppliers import SupplierBehaviour

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        led.record_purchase(
            supplier_name=supplier,
            taxable_value=int(round(float(rupees) * 100)),
            rate_bps=1800, behaviour=SupplierBehaviour(behaviour), paid=True)

def _check(client, timeout=20):
    import merchant.app as appmod

    r = client.post("/suppliers/check", data={"use_agent": "no"},
                    follow_redirects=False)
    key = r.headers["location"].rsplit("/", 1)[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._lock:
            state = dict(appmod.RUNS.get(key) or {})
        if state.get("state") != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("the check never finished")


def _led():
    import merchant.app as appmod

    led = appmod.ledger()
    led.business_id = led.businesses.all()[0]["business_id"]
    return led


# --- it has to remember ---------------------------------------------------

def test_the_first_check_has_nothing_to_compare_against(shop):
    _buy(shop, "Anand Textiles")
    state = _check(shop)
    text = " ".join(l["text"] for l in state["lines"])
    assert "first check" in text
    assert "nothing to compare" in text


def test_the_first_check_still_records_the_baseline(shop):
    """Without this the second check has nothing to diff against either."""
    _buy(shop, "Anand Textiles")
    _check(shop)
    with _led() as led:
        assert led.last_check() is not None
        assert led.supplier_register()


def test_a_second_check_compares_against_the_first(shop):
    _buy(shop, "Anand Textiles")
    _check(shop)
    _buy(shop, "Deepak Packaging", behaviour="not_filed")
    state = _check(shop)
    text = " ".join(l["text"] for l in state["lines"])
    assert "moved since last time" in text


def test_a_check_with_nothing_new_says_so(shop):
    _buy(shop, "Anand Textiles")
    _check(shop)
    state = _check(shop)
    text = " ".join(l["text"] for l in state["lines"])
    assert "Nothing changed" in text


def test_every_check_is_kept(shop):
    _buy(shop, "Anand Textiles")
    for _ in range(3):
        _check(shop)
    with _led() as led:
        assert len(led.watch_checks()) == 3


# --- the register ---------------------------------------------------------

def test_the_register_ranks_by_exposure(shop):
    _buy(shop, "Big Default Ltd", "200000", behaviour="not_filed")
    _buy(shop, "Small Default Ltd", "20000", behaviour="not_filed")
    _buy(shop, "Reliable Ltd", "500000")
    _check(shop)
    with _led() as led:
        register = led.supplier_register()
    exposures = [r["exposed_paise"] for r in register]
    assert exposures == sorted(exposures, reverse=True)
    assert register[0]["name"] == "Big Default Ltd"


def test_a_supplier_who_filed_everything_is_exposed_for_nothing(shop):
    _buy(shop, "Reliable Ltd")
    _check(shop)
    with _led() as led:
        assert led.supplier_register()[0]["exposed_paise"] == 0


def test_a_supplier_nobody_looked_up_is_recorded_as_unchecked(shop):
    """Not "active". We have not looked, and that must not read as a clean
    bill of health."""
    _buy(shop, "Anand Textiles")
    _check(shop)
    with _led() as led:
        assert led.supplier_register()[0]["status"] == STATUS_UNKNOWN


def test_the_page_shows_the_register(shop):
    _buy(shop, "Deepak Packaging", behaviour="not_filed")
    _check(shop)
    page = shop.get("/suppliers").text
    assert "Deepak Packaging" in page
    assert "Who is holding your credit" in page


def test_the_page_refuses_to_call_a_filing_rate_a_prediction(shop):
    """
    The wording matters. A percentage that reads as a forecast is the exact
    unfalsifiable number CLAUDE.md section 3 rules out.
    """
    _buy(shop, "Anand Textiles")
    _check(shop)
    page = shop.get("/suppliers").text
    assert "not a prediction" in page


# --- what it decided not to raise is kept too -----------------------------

def test_decisions_not_to_raise_are_recorded(shop):
    """
    "It stayed quiet about eleven things" is only a claim you can make if you
    wrote the quiet ones down.
    """
    from agent.watch_agent import Raised

    _buy(shop, "Anand Textiles")
    _check(shop)
    with _led() as led:
        led.record_check(
            {}, [Raised(kind="first_seen", gstin="X", name="Quiet Ltd",
                        raise_it=False, urgency="no_action", action="nothing",
                        headline="not worth mentioning", reasoning="")],
            period="2026-08", used_agent=True)
        check_id = led.last_check()["check_id"]
        assert led.raised_in(check_id, only_raised=True) == []
        assert len(led.raised_in(check_id, only_raised=False)) == 1


# --- guards ---------------------------------------------------------------

def test_checking_with_no_purchases_is_refused(shop):
    r = shop.post("/suppliers/check", follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_checking_with_the_agent_switched_off_is_refused(shop):
    _buy(shop, "Anand Textiles")
    shop.post("/agents/gst_itc/toggle")
    r = shop.post("/suppliers/check", follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_one_business_cannot_see_another_businesses_suppliers(shop):
    _buy(shop, "Anand Textiles")
    _check(shop)
    shop.post("/businesses", data={"name": "Other Shop"})
    page = shop.get("/suppliers").text
    assert "Anand Textiles" not in page


def test_the_watch_is_not_registered_as_a_separate_agent():
    """
    It is the reconciler doing another job on the same data. A catalogue entry
    would inflate the agent count without adding a capability.

    Asserts the watch is absent rather than enumerating every live agent -
    that spelling meant adding a genuinely new agent broke a test about the
    watch, which is a test failing for a reason it is not about.
    """
    import merchant.app  # noqa: F401
    from merchant.catalog import live_agents

    ids = {a.id for a in live_agents()}
    for invented in ("supplier_watch", "watch", "gst_watch"):
        assert invented not in ids
    assert "gst_itc" in ids, "the watch belongs to the reconciler"


def test_the_most_urgent_finding_is_shown_first(shop):
    """
    Regression. The page ordered by rupees, so a "this week" item outranked a
    "do this now" one whenever it happened to be larger - throwing away the
    exact judgment the agent had just made. A cancelled registration accruing
    18% interest beats a bigger sum with a year left to claim.
    """
    from agent.watch_agent import Raised

    _buy(shop, "Anand Textiles")
    _check(shop)

    with _led() as led:
        led.record_check({}, [
            Raised(kind="exposure_rose", gstin="A", name="Big But Patient",
                   raise_it=True, urgency="this_month", action="chase_supplier",
                   headline="big", reasoning="", exposed_paise=90_000_00),
            Raised(kind="stopped_filing", gstin="B", name="Medium Soon",
                   raise_it=True, urgency="this_week", action="chase_supplier",
                   headline="medium", reasoning="", exposed_paise=50_000_00),
            Raised(kind="registration_died", gstin="C", name="Small But Bleeding",
                   raise_it=True, urgency="now", action="reverse_claim",
                   headline="small", reasoning="", exposed_paise=10_000_00),
        ], period="2026-08", used_agent=True)
        rows = led.raised_in(led.last_check()["check_id"])

    assert [r["urgency"] for r in rows] == ["now", "this_week", "this_month"]
    assert rows[0]["name"] == "Small But Bleeding"


def test_within_one_urgency_the_larger_sum_comes_first(shop):
    from agent.watch_agent import Raised

    _buy(shop, "Anand Textiles")
    _check(shop)
    with _led() as led:
        led.record_check({}, [
            Raised(kind="stopped_filing", gstin="A", name="Smaller",
                   raise_it=True, urgency="now", action="chase_supplier",
                   headline="s", reasoning="", exposed_paise=10_000_00),
            Raised(kind="stopped_filing", gstin="B", name="Larger",
                   raise_it=True, urgency="now", action="chase_supplier",
                   headline="l", reasoning="", exposed_paise=90_000_00),
        ], period="2026-08", used_agent=True)
        rows = led.raised_in(led.last_check()["check_id"])
    assert [r["name"] for r in rows] == ["Larger", "Smaller"]
