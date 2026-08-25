"""
Tests for the Zoho Books connector.

There is no Zoho sandbox, so every one of these runs against a stub. That is
not a compromise - a connector nobody can exercise until demo day is a
connector that fails on demo day, which is why ZohoBooks takes its http
callable as an argument.

What is actually being protected here: secrets that must never land in the
clear, a callback that must not accept somebody else's authorisation, and an
importer that must not manufacture a supplier default out of missing data.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.vault import Vault  # noqa: E402
from merchant.zoho import (DEFAULT_REGION, REGIONS, SCOPES,  # noqa: E402
                           ZohoBooks, ZohoConnections, ZohoError,
                           authorise_url, importable, to_purchase)


class Reply:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def stub(routes):
    """An http callable that answers from a dict of url-fragment -> payload."""
    calls = []

    def http(method, url, **kw):
        calls.append((method, url, kw))
        for fragment, reply in routes.items():
            if fragment in url:
                return reply if isinstance(reply, Reply) else Reply(reply)
        return Reply({"message": "not found"}, 404)

    http.calls = calls
    return http


TOKEN = {"access_token": "tok-123", "expires_in": 3600}


def _client(routes, **kw):
    base = dict(client_id="1000.ABC", client_secret="shh",
                refresh_token="refresh-abc", organization_id="60000",
                region="in")
    base.update(kw)
    return ZohoBooks(http=stub({"oauth/v2/token": TOKEN, **routes}), **base)


# --- the authorisation link ----------------------------------------------

def test_the_authorise_link_asks_only_for_read_access():
    """
    Guardrail 1 says the agent never writes to a ledger, and somebody else's
    accounting system is very much a ledger. Enforced by the scope, not by
    intent.
    """
    url = authorise_url("1000.ABC", "http://localhost/cb")
    assert "READ" in url
    for verb in ("CREATE", "UPDATE", "DELETE", "WRITE"):
        assert verb not in SCOPES


def test_the_link_asks_for_offline_access():
    """Without it Zoho returns no refresh token and every pull needs a human."""
    assert "access_type=offline" in authorise_url("1000.ABC", "http://x/cb")


def test_the_link_points_at_the_right_data_centre():
    assert "accounts.zoho.in" in authorise_url("x", "http://x/cb", "in")
    assert "accounts.zoho.eu" in authorise_url("x", "http://x/cb", "eu")


def test_an_unknown_region_falls_back_rather_than_crashing():
    assert "accounts.zoho.in" in authorise_url("x", "http://x/cb", "mars")


# --- talking to Zoho ------------------------------------------------------

def test_a_refused_refresh_token_is_reported_not_swallowed():
    client = ZohoBooks(client_id="a", client_secret="b", refresh_token="stale",
                       organization_id="1",
                       http=stub({"oauth/v2/token": Reply({"error": "invalid"}, 400)}))
    with pytest.raises(ZohoError) as caught:
        client.vendors()
    assert "Reconnect" in str(caught.value)


def test_an_expired_token_is_renewed_once_mid_run():
    """Access tokens last an hour; a long pull can outlive one."""
    state = {"first": True}

    def http(method, url, **kw):
        if "oauth/v2/token" in url:
            return Reply(TOKEN)
        if state["first"]:
            state["first"] = False
            return Reply({"message": "expired"}, 401)
        return Reply({"contacts": [], "page_context": {"has_more_page": False}})

    client = ZohoBooks(client_id="a", client_secret="b", refresh_token="r",
                       organization_id="1", http=http)
    assert client.vendors() == []


def test_check_names_the_organisation():
    client = _client({"organizations": {"organizations": [
        {"organization_id": "60000", "name": "Meera Boutique Pvt Ltd"}]}})
    result = client.check()
    assert result.ok
    assert "Meera Boutique" in result.message


def test_check_fails_when_the_organisation_id_is_wrong():
    client = _client({"organizations": {"organizations": [
        {"organization_id": "99999", "name": "Someone Else"}]}})
    result = client.check()
    assert not result.ok
    assert "60000" in result.message


def test_paging_follows_has_more_page():
    pages = [
        {"contacts": [{"contact_id": "1"}], "page_context": {"has_more_page": True}},
        {"contacts": [{"contact_id": "2"}], "page_context": {"has_more_page": False}},
    ]

    def http(method, url, **kw):
        if "oauth/v2/token" in url:
            return Reply(TOKEN)
        return Reply(pages[kw["params"]["page"] - 1])

    client = ZohoBooks(client_id="a", client_secret="b", refresh_token="r",
                       organization_id="1", http=http)
    assert len(client.vendors()) == 2


# --- mapping a bill -------------------------------------------------------

BILL = {
    "bill_id": "99", "vendor_id": "7", "vendor_name": "Anand Textiles",
    "bill_number": "ANA/3768", "date": "2026-06-16", "sub_total": 231766.0,
    "gst_no": "27VLBAN4982B2ZX", "last_payment_date": "2026-07-01",
    "taxes": [{"tax_name": "CGST9", "tax_amount": 20858.94},
              {"tax_name": "SGST9", "tax_amount": 20858.94}],
}


def test_money_arrives_as_integer_paise():
    purchase = to_purchase(BILL)
    for key in ("taxable_value", "cgst", "sgst", "igst"):
        assert isinstance(purchase[key], int)
    assert purchase["taxable_value"] == 23_176_600
    assert purchase["cgst"] == 2_085_894


def test_the_tax_split_is_read_not_guessed():
    purchase = to_purchase(BILL)
    assert purchase["cgst"] and purchase["sgst"]
    assert purchase["igst"] == 0


def test_an_interstate_bill_lands_in_igst():
    bill = {**BILL, "taxes": [{"tax_name": "IGST18", "tax_amount": 41717.88}]}
    purchase = to_purchase(bill)
    assert purchase["igst"] == 4_171_788
    assert purchase["cgst"] == purchase["sgst"] == 0


def test_a_gstin_on_the_vendor_is_used_when_the_bill_has_none():
    bill = {k: v for k, v in BILL.items() if k != "gst_no"}
    purchase = to_purchase(bill, {"7": {"gst_no": "27AAAAA0000A1Z5"}})
    assert purchase["supplier_gstin"] == "27AAAAA0000A1Z5"


# --- what must not be imported --------------------------------------------

def test_a_bill_with_no_gstin_is_refused():
    """
    There is nothing to join it to GSTR-2B on, so it would sit in the register
    permanently unfiled - which reads as a supplier default when it is actually
    missing data. A false accusation manufactured by our own importer.
    """
    purchase = to_purchase({k: v for k, v in BILL.items() if k != "gst_no"})
    assert importable(purchase) is not None


def test_a_malformed_gstin_is_refused():
    purchase = to_purchase({**BILL, "gst_no": "27ABC"})
    assert "15 characters" in importable(purchase)


def test_a_bill_with_no_number_or_date_is_refused():
    assert importable(to_purchase({**BILL, "bill_number": "",
                                   "reference_number": "", "bill_id": ""}))
    assert importable(to_purchase({**BILL, "date": None}))


def test_a_complete_bill_is_importable():
    assert importable(to_purchase(BILL)) is None


# --- secrets --------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("LEDGERLINE_SECRET_KEY", Vault.generate_key())
    conn = sqlite3.connect(tmp_path / "z.db")
    conn.row_factory = sqlite3.Row
    return ZohoConnections(conn)


def test_connecting_without_an_encryption_key_is_refused(tmp_path, monkeypatch):
    """
    The middle option - storing it in the clear because storing it was
    convenient - is the one that never happens.
    """
    import sqlite3

    monkeypatch.delenv("LEDGERLINE_SECRET_KEY", raising=False)
    conn = sqlite3.connect(tmp_path / "z.db")
    conn.row_factory = sqlite3.Row
    assert ZohoConnections(conn).begin(
        "biz", client_id="a", client_secret="b", organization_id="1",
        region="in") is None


def test_neither_secret_is_ever_stored_in_the_clear(store):
    store.begin("biz", client_id="1000.ABC", client_secret="super-secret",
                organization_id="60000", region="in")
    store.complete("biz", "refresh-token-value")
    row = dict(store.get("biz"))
    assert "super-secret" not in json.dumps(row)
    assert "refresh-token-value" not in json.dumps(row)


def test_there_is_no_column_that_could_hold_a_plaintext_secret(store):
    columns = {c[1] for c in store.conn.execute(
        "PRAGMA table_info(zoho_connections)")}
    for name in ("client_secret", "refresh_token", "password", "token"):
        assert name not in columns, f"{name} could hold a secret in the clear"


def test_no_client_exists_until_the_merchant_has_authorised(store):
    store.begin("biz", client_id="a", client_secret="b", organization_id="1",
                region="in")
    assert store.client("biz") is None


def test_disconnecting_removes_everything(store):
    store.begin("biz", client_id="a", client_secret="b", organization_id="1",
                region="in")
    store.complete("biz", "refresh")
    store.disconnect("biz")
    assert store.get("biz") is None


def test_a_state_token_is_minted_per_connection_attempt(store):
    first = store.begin("biz", client_id="a", client_secret="b",
                        organization_id="1", region="in")
    second = store.begin("biz", client_id="a", client_secret="b",
                         organization_id="1", region="in")
    assert first and second and first != second


def test_completing_clears_the_state_token(store):
    """A state token that survives is a token that can be replayed."""
    store.begin("biz", client_id="a", client_secret="b", organization_id="1",
                region="in")
    store.complete("biz", "refresh")
    assert store.get("biz")["state_token"] is None


# --- the callback must not accept somebody else's authorisation ----------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("LEDGERLINE_SECRET_KEY", Vault.generate_key())
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in",
                                 "password": "a-good-password"})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    return client


def _begin(client):
    client.post("/zoho/begin", data={
        "client_id": "1000.ABC", "client_secret": "shh",
        "organization_id": "60000", "region": "in"}, follow_redirects=False)
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        return ZohoConnections(led.conn).get(biz)["state_token"]


def test_a_callback_with_the_wrong_state_is_refused(shop):
    """
    Without this check, anyone could send a logged-in owner to this URL
    carrying a code from THEIR Zoho account, and this business would quietly
    start reading somebody else's books.
    """
    _begin(shop)
    r = shop.get("/zoho/callback?code=abc&state=not-the-one",
                 follow_redirects=False)
    assert "error=" in r.headers["location"]

    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        assert ZohoConnections(led.conn).client(biz) is None


def test_a_refused_callback_is_recorded_as_a_denial(shop):
    import merchant.app as appmod

    _begin(shop)
    shop.get("/zoho/callback?code=abc&state=wrong", follow_redirects=False)
    with appmod.ledger() as led:
        denied = led.conn.execute(
            "SELECT * FROM access_log WHERE outcome = 'denied'"
            " AND action = 'connect_source'").fetchall()
    assert denied
    assert "state did not match" in denied[0]["detail"]


def test_a_callback_with_no_connection_in_progress_is_refused(shop):
    r = shop.get("/zoho/callback?code=abc&state=anything",
                 follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_zoho_returning_an_error_is_shown_not_swallowed(shop):
    _begin(shop)
    r = shop.get("/zoho/callback?error=access_denied", follow_redirects=False)
    assert "access_denied" in r.headers["location"]


def test_only_an_owner_may_connect_an_accounting_system(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import merchant.app as appmod
    from merchant.auth import Auth, Role

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "owner@x.in",
                                 "password": "a-good-password"})
    client.post("/businesses", data={"name": "Shop"})
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", "a-good-password")
        auth.add_member(biz, staff.user_id, Role.STAFF)
    client.post("/login", data={"email": "staff@x.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")
    assert client.get("/zoho").status_code == 403


def test_an_imported_purchase_does_not_get_a_simulated_supplier(tmp_path):
    """
    record_purchase asks the simulator what the supplier files, which is right
    for demo data and wrong for a real bill. Inventing a filing status for a
    supplier who actually exists would mean the reconciler grading data we made
    up about a real company.
    """
    from merchant.ledger import Ledger

    led = Ledger(str(tmp_path / "l.db"))
    led.business_id = led.businesses.create("Shop")
    led.record_zoho_purchase(to_purchase(BILL))

    assert led.purchases()[0]["behaviour"] == "imported"
    assert led.conn.execute(
        "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 0
    led.close()
