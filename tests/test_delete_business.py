"""
Tests for removing a business.

The design decision under test is that "delete" and "archive" are not two
buttons the user picks between - which one they are offered is decided by
whether the business has ever been audited.

A business that has been audited holds findings, the reasoning behind each one,
and the log of what the agent decided. Guardrail 5 promises every agent
decision is replayable. A delete button that erased them on request would make
that promise conditional on nobody pressing it, which is the same as not making
it. So an audited business can be put away and not destroyed, and the refusal
is enforced in the data layer as well as the route.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.accesslog import Action  # noqa: E402
from merchant.auth import Auth, Role  # noqa: E402

PASSWORD = "a-good-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def owner(client):
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    client.post("/sources/simulator")
    return client


def _ledger():
    import merchant.app as appmod

    return appmod.ledger()


def _only_business():
    with _ledger() as led:
        return led.businesses.all(include_archived=True)[0]["business_id"]


def _exists(business_id):
    with _ledger() as led:
        return led.businesses.get(business_id) is not None


def _audited(client):
    """Give the business a settlement, without spending anything on the agent."""
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle", data={"use_agent": "no"})
    with _ledger() as led:
        assert led.businesses.settlement_count(_only_business()) > 0


# --- which door you are offered ------------------------------------------

def test_a_business_that_was_never_audited_may_be_deleted(owner):
    with _ledger() as led:
        assert led.businesses.may_delete(_only_business())


def test_a_business_that_was_audited_may_not_be_deleted(owner):
    _audited(owner)
    with _ledger() as led:
        assert not led.businesses.may_delete(_only_business())


def test_settings_offers_delete_before_any_audit(owner):
    page = owner.get("/settings").text
    assert "Delete business" in page
    assert "Archive business" not in page


def test_settings_offers_archive_once_audited(owner):
    _audited(owner)
    page = owner.get("/settings").text
    assert "Archive business" in page
    assert "Delete business" not in page


# --- deleting ------------------------------------------------------------

def test_deleting_removes_the_business(owner):
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    assert not _exists(bid)


def test_deleting_takes_its_rate_card_and_membership_with_it(owner):
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    with _ledger() as led:
        for table in ("business_rate_card", "memberships", "business_agents",
                      "live_payments", "data_sources"):
            left = led.conn.execute(
                f"SELECT COUNT(*) n FROM {table} WHERE business_id = ?",
                (bid,)).fetchone()["n"]
            assert left == 0, f"{table} still holds rows for a deleted business"


def test_the_wrong_name_deletes_nothing(owner):
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": "Some Other Shop"})
    assert _exists(bid)


def test_an_empty_confirmation_deletes_nothing(owner):
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": ""})
    assert _exists(bid)


def test_the_confirmation_forgives_case_and_spaces(owner):
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": "  meera's BOUTIQUE "})
    assert not _exists(bid)


def test_deleting_the_business_you_are_standing_in_does_not_strand_you(owner):
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    assert owner.get("/businesses").status_code == 200
    assert owner.get("/").status_code == 200


# --- the refusal ---------------------------------------------------------

def test_an_audited_business_survives_the_delete_route(owner):
    _audited(owner)
    bid = _only_business()
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    assert _exists(bid)


def test_the_data_layer_refuses_too_not_just_the_route(owner):
    """
    The route checks first so it can offer archiving instead. This check exists
    so a future caller that forgets cannot destroy an audit trail.
    """
    _audited(owner)
    bid = _only_business()
    with _ledger() as led:
        with pytest.raises(ValueError):
            led.businesses.delete(bid)
    assert _exists(bid)


def test_archiving_leaves_the_settlement_and_its_findings_untouched(owner):
    """
    The whole reason an audited business is archived rather than deleted. The
    finding is planted directly instead of driving the auditor, which runs in a
    background thread and is tested on its own elsewhere.
    """
    _audited(owner)
    bid = _only_business()
    with _ledger() as led:
        run_id = led.conn.execute(
            "SELECT run_id FROM business_runs WHERE business_id = ?",
            (bid,)).fetchone()["run_id"]
        led.conn.execute(
            "INSERT INTO variances (payment_id, run_id, expected_fee,"
            " actual_fee, expected_tax, actual_tax, delta, exception_code,"
            " confidence, reasoning, created_at)"
            " VALUES ('pay_x', ?, 0, 6100, 0, 1098, 7198,"
            " 'ZERO_MDR_VIOLATION', 0.96, 'network MDR on UPI', 0)", (run_id,))
        led.conn.commit()
        lines_before = led.conn.execute(
            "SELECT COUNT(*) n FROM settlement_lines WHERE run_id = ?",
            (run_id,)).fetchone()["n"]

    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})

    with _ledger() as led:
        assert led.businesses.is_archived(bid)
        found = led.conn.execute(
            "SELECT * FROM variances WHERE run_id = ?", (run_id,)).fetchall()
        assert len(found) == 1
        assert found[0]["exception_code"] == "ZERO_MDR_VIOLATION"
        assert found[0]["delta"] == 7198
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM settlement_lines WHERE run_id = ?",
            (run_id,)).fetchone()["n"] == lines_before
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM business_runs WHERE business_id = ?",
            (bid,)).fetchone()["n"] == 1


# --- archiving -----------------------------------------------------------

def test_archiving_hides_it_from_the_switcher(owner):
    _audited(owner)
    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})
    with _ledger() as led:
        assert led.businesses.all() == []
        assert len(led.businesses.all(include_archived=True)) == 1


def test_an_archived_business_cannot_be_opened(owner):
    """Switching into one sends you back to the list rather than inside it."""
    _audited(owner)
    bid = _only_business()
    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})
    back = owner.get(f"/switch?business_id={bid}", follow_redirects=False)
    assert back.headers["location"] == "/businesses"
    assert "business_id" not in back.cookies


def test_archiving_the_wrong_name_changes_nothing(owner):
    _audited(owner)
    bid = _only_business()
    owner.post("/settings/archive", data={"confirm": "Not The Name"})
    with _ledger() as led:
        assert not led.businesses.is_archived(bid)


def test_an_archived_business_can_be_restored(owner):
    _audited(owner)
    bid = _only_business()
    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})
    owner.post("/businesses/restore", data={"business_id": bid})
    with _ledger() as led:
        assert not led.businesses.is_archived(bid)


def test_the_archived_section_appears_only_when_something_is_in_it(owner):
    assert "Archived" not in owner.get("/businesses").text
    _audited(owner)
    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})
    assert "Archived" in owner.get("/businesses").text


# --- who may do it -------------------------------------------------------

def test_staff_cannot_delete_a_business(client):
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    bid = _only_business()
    with _ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", PASSWORD)
        auth.add_member(bid, staff.user_id, Role.STAFF)
    client.post("/login", data={"email": "staff@x.in", "password": PASSWORD})
    client.get(f"/switch?business_id={bid}")
    client.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    assert _exists(bid)


def test_a_stranger_cannot_delete_a_business(client):
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    bid = _only_business()
    client.post("/signup", data={"email": "nosy@x.in", "password": PASSWORD})
    client.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    assert _exists(bid)


# --- and it is recorded --------------------------------------------------

def test_the_deletion_outlives_the_business_it_describes(owner):
    """
    The business's own access log is deleted with it, so a record filed under
    the business would delete itself. It is filed against the person instead.
    """
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    with _ledger() as led:
        rows = led.conn.execute(
            "SELECT * FROM access_log WHERE action = ?",
            (str(Action.DELETE_BUSINESS),)).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "allowed"
    assert rows[0]["email"] == "meera@x.in"
    assert rows[0]["business_id"] is None
    assert "Meera's Boutique" in rows[0]["detail"]


def test_a_refused_deletion_is_recorded(owner):
    _audited(owner)
    owner.post("/settings/delete", data={"confirm": "Meera's Boutique"})
    with _ledger() as led:
        rows = led.conn.execute(
            "SELECT * FROM access_log WHERE action = ? AND outcome = 'denied'",
            (str(Action.DELETE_BUSINESS),)).fetchall()
    assert rows and "settlements" in rows[0]["detail"]


def test_archiving_is_recorded(owner):
    _audited(owner)
    owner.post("/settings/archive", data={"confirm": "Meera's Boutique"})
    with _ledger() as led:
        rows = led.conn.execute(
            "SELECT * FROM access_log WHERE action = ?",
            (str(Action.ARCHIVE_BUSINESS),)).fetchall()
    assert rows and "archived" in rows[0]["detail"]


def test_a_blocked_staff_attempt_is_logged_as_what_it_was(client):
    """
    require_owner used to hardcode "changed the rate card". A log that
    describes a blocked deletion as a rate-card edit is a false entry.
    """
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    bid = _only_business()
    with _ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", PASSWORD)
        auth.add_member(bid, staff.user_id, Role.STAFF)
    client.post("/login", data={"email": "staff@x.in", "password": PASSWORD})
    client.get(f"/switch?business_id={bid}")
    client.post("/settings/delete", data={"confirm": "Meera's Boutique"})

    with _ledger() as led:
        rows = led.conn.execute(
            "SELECT action FROM access_log WHERE email = 'staff@x.in'"
            " AND outcome = 'denied'").fetchall()
    actions = {r["action"] for r in rows}
    assert str(Action.DELETE_BUSINESS) in actions
    assert str(Action.CHANGE_RATE_CARD) not in actions
