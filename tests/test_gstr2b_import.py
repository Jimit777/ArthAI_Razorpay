"""
Tests for reading a real GSTR-2B download.

The thing this has to survive is that GSTN has shipped the same data in
several shapes. A parser that insists on one produces "no invoices found" on a
file that is perfectly valid, and the merchant has no way to tell whether the
problem is their file or our code.
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.gstr2b_import import parse, parse_many  # noqa: E402

PASSWORD = "a-good-password"


def _doc(b2b, *, path=("data", "docdata", "b2b"), rtnprd="072026",
         gstin="27MERCH1234A1Z5"):
    """Build a GSTR-2B payload with the B2B section at a given nesting."""
    node = b2b
    for key in reversed(path[1:]):
        node = {key: node}
    body = {"gstin": gstin, "rtnprd": rtnprd}
    body.update(node if path[0] != "data" else node)
    return {"data": body} if path[0] == "data" else {path[0]: node}


FLAT_SUPPLIER = [{
    "ctin": "29AABCU1332L1ZZ", "trdnm": "Anand Textiles",
    "supprd": "062026", "supfildt": "11-07-2026",
    "inv": [{"inum": "ANA/2041", "idt": "15-06-2026", "val": 141600,
             "txval": 120000, "igst": 21600, "cgst": 0, "sgst": 0,
             "itcavl": "Y"}]}]


# --- the shapes GSTN actually ships ---------------------------------------

def test_tax_flattened_onto_the_invoice_is_read():
    result = parse(json.dumps(_doc(FLAT_SUPPLIER)).encode())
    assert result.ok
    assert result.lines[0].igst == 21_60_000
    assert result.lines[0].taxable_value == 1_20_00_000


def test_tax_nested_under_items_is_read():
    """GSTR-1's shape, which turns up in 2B files people actually have."""
    nested = [{"ctin": "27XJGQI1052H7ZR", "trdnm": "Coimbatore Yarns",
               "inv": [{"inum": "COI/905", "idt": "14-06-2026", "itms": [
                   {"num": 1, "itm_det": {"txval": 90000, "camt": 8100,
                                          "samt": 8100, "iamt": 0}},
                   {"num": 2, "itm_det": {"txval": 90000, "camt": 8100,
                                          "samt": 8100, "iamt": 0}}]}]}]
    result = parse(json.dumps(_doc(nested)).encode())
    assert result.ok
    line = result.lines[0]
    assert line.taxable_value == 1_80_00_000
    assert line.cgst == line.sgst == 16_20_000


@pytest.mark.parametrize("path", [
    ("data", "docdata", "b2b"),
    ("data", "b2b"),
    ("data", "docsumm", "b2b"),
])
def test_the_b2b_section_is_found_wherever_it_sits(path):
    result = parse(json.dumps(_doc(FLAT_SUPPLIER, path=path)).encode())
    assert result.ok, path


def test_both_period_orderings_are_understood():
    """
    "072026" and "202607" both mean July 2026, and both turn up. Guessing
    wrong shifts every finding by years.
    """
    assert parse(json.dumps(_doc(FLAT_SUPPLIER, rtnprd="072026")).encode()
                 ).period == "2026-07"
    assert parse(json.dumps(_doc(FLAT_SUPPLIER, rtnprd="202607")).encode()
                 ).period == "2026-07"


def test_gst_date_format_is_read():
    result = parse(json.dumps(_doc(FLAT_SUPPLIER)).encode())
    assert result.lines[0].invoice_date == date(2026, 6, 15)


# --- what the government itself says --------------------------------------

def test_gstn_saying_credit_is_unavailable_is_carried_through():
    """
    GSTN marks each invoice with whether ITC is available and why not. That is
    their opinion about the credit and the merchant needs it regardless of
    what our own rules conclude.
    """
    blocked = [{"ctin": "29AABCU1332L1ZZ", "trdnm": "Anand",
                "inv": [{"inum": "A/1", "idt": "15-06-2026", "txval": 60000,
                         "cgst": 5400, "sgst": 5400, "itcavl": "N",
                         "rsn": "POS and supplier state are same"}]}]
    result = parse(json.dumps(_doc(blocked)).encode())
    assert len(result.blocked_by_gstn) == 1
    assert "POS" in result.lines[0].itc_unavailable_reason


def test_credit_notes_and_amendments_are_counted_not_folded_in():
    """
    They belong in a full reconciliation and neither is what the engine joins
    on. Counting them beats silently treating a credit note as an invoice.
    """
    payload = _doc(FLAT_SUPPLIER)
    payload["data"]["docdata"]["cdnr"] = [{"ctin": "X"}, {"ctin": "Y"}]
    result = parse(json.dumps(payload).encode())
    assert result.other_sections.get("cdnr") == 2


# --- when a file is not what it claims ------------------------------------

def test_a_file_that_is_not_json_says_so_usefully():
    result = parse(b"<html>not json</html>", "2b.xlsx")
    assert not result.ok
    assert "JSON version" in result.error


def test_a_json_file_with_no_b2b_section_says_what_to_check():
    result = parse(json.dumps({"data": {"gstin": "27X"}}).encode())
    assert not result.ok
    assert "2A" in result.error or "B2B" in result.error


def test_an_empty_b2b_section_is_not_an_error_about_our_code():
    result = parse(json.dumps(_doc([])).encode())
    assert "no supplier reported" in result.error


def test_an_invoice_with_no_number_is_named_not_dropped():
    broken = [{"ctin": "29AABCU1332L1ZZ", "trdnm": "Anand",
               "inv": [{"idt": "15-06-2026", "txval": 1000, "igst": 180}]}]
    result = parse(json.dumps(_doc(broken)).encode())
    assert result.skipped and "Anand" in result.skipped[0]


# --- several months at once -----------------------------------------------

def test_several_periods_come_back_in_order():
    files = [(json.dumps(_doc(FLAT_SUPPLIER, rtnprd=p)).encode(), f"{p}.json")
             for p in ("092026", "072026", "082026")]
    parsed, problems = parse_many(files)
    assert [p.period for p in parsed] == ["2026-07", "2026-08", "2026-09"]
    assert not problems


def test_the_same_period_twice_is_reported_not_doubled():
    files = [(json.dumps(_doc(FLAT_SUPPLIER)).encode(), "a.json"),
             (json.dumps(_doc(FLAT_SUPPLIER)).encode(), "b.json")]
    parsed, problems = parse_many(files)
    assert len(parsed) == 1
    assert problems and "two files cover" in problems[0]


def test_one_bad_file_does_not_lose_the_good_ones():
    files = [(json.dumps(_doc(FLAT_SUPPLIER)).encode(), "good.json"),
             (b"rubbish", "bad.json")]
    parsed, problems = parse_many(files)
    assert len(parsed) == 1
    assert problems and "bad.json" in problems[0]


# --- storing it -----------------------------------------------------------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    client.post("/sources/simulator")
    return client


def _upload(client, *periods):
    files = [("gstr2b", (f"{p}.json",
                         json.dumps(_doc(FLAT_SUPPLIER, rtnprd=p)).encode(),
                         "application/json")) for p in periods]
    return client.post("/agents/input-credit/gstr2b", files=files,
                       follow_redirects=False)


def test_uploading_stores_the_lines(shop):
    import merchant.app as appmod

    r = _upload(shop, "072026")
    assert "ok=" in r.headers["location"]
    with appmod.ledger() as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 1


def test_importing_a_period_twice_replaces_rather_than_duplicates(shop):
    """
    GSTR-2B is a static statement - it does not change after generation - so a
    second copy of the same month is a duplicate, never an update.
    """
    import merchant.app as appmod

    _upload(shop, "072026")
    _upload(shop, "072026")
    with appmod.ledger() as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 1


def test_several_months_can_be_imported_in_one_go(shop):
    import merchant.app as appmod

    _upload(shop, "052026", "062026", "072026")
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        assert len(led.gstr2b_periods()) == 3


def test_the_page_says_how_much_history_is_held(shop):
    _upload(shop, "052026", "062026", "072026")
    page = shop.get("/agents/input-credit").text
    assert "3 periods imported" in page
    assert "Enough history for the supplier watch" in page


def test_the_page_says_when_there_is_not_enough_yet(shop):
    _upload(shop, "072026")
    page = shop.get("/agents/input-credit").text
    assert "2 more periods" in page


def test_the_page_explains_where_to_get_the_file(shop):
    page = shop.get("/agents/input-credit").text
    assert "Return Dashboard" in page
    assert "JSON" in page


def test_only_an_owner_may_import(tmp_path, monkeypatch):
    import merchant.app as appmod
    from merchant.auth import Auth, Role

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "owner@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Shop"})
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", PASSWORD)
        auth.add_member(biz, staff.user_id, Role.STAFF)
    client.post("/login", data={"email": "staff@x.in", "password": PASSWORD})
    client.get(f"/switch?business_id={biz}")
    assert _upload(client, "072026").status_code == 403
