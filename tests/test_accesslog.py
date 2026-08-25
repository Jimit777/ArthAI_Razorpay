"""
Tests for the access log.

Guardrail 5 already logs what the AGENT decided. This is the other half: what
PEOPLE saw. The design decision worth defending is that a log recording only
successes misses the interesting event - someone reading their own settlement
is routine; someone trying to reach a business they are not in is the thing you
would actually want to know about, and a success-only log throws it away.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.accesslog import AccessLog, Action  # noqa: E402
from merchant.auth import Auth, Role  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


def _owner(client, email="owner@x.in"):
    client.post("/signup", data={"email": email, "password": "a-good-password"})
    client.post("/businesses", data={"name": "Boutique"})
    client.post("/sources/simulator")


def _entries(client):
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        return AccessLog(led.conn).for_business(biz), biz


# --- append only ---------------------------------------------------------

def test_nothing_in_the_codebase_updates_or_deletes_the_log():
    """
    SQLite cannot enforce append-only, so this is enforced by discipline and
    checked here. A log that can be edited is not an audit log, it is a list.
    """
    root = Path(__file__).parent.parent
    for path in list(root.glob("*.py")) + list(root.glob("merchant/*.py")) \
            + list(root.glob("engine/*.py")) + list(root.glob("agent/*.py")):
        text = path.read_text().lower()
        for forbidden in ("update access_log", "delete from access_log"):
            assert forbidden not in text, f"{path.name} writes: {forbidden}"


def test_the_log_class_exposes_no_way_to_change_a_row():
    public = {m for m in dir(AccessLog) if not m.startswith("_")}
    assert public == {"record", "denied", "for_business", "denials", "counts"}


# --- what gets recorded --------------------------------------------------

def test_opening_a_settlement_is_recorded(client):
    _owner(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.get(f"/settlements/{run_id}")

    entries, _ = _entries(client)
    viewed = [e for e in entries if e.action == Action.VIEW_SETTLEMENT]
    assert viewed
    assert viewed[0].target == run_id
    assert viewed[0].email == "owner@x.in"
    assert not viewed[0].denied


def test_asking_the_agent_is_recorded_with_the_question(client):
    """The agent reads this business's books to answer. That is access."""
    _owner(client)
    client.post("/ask", data={"question": "Why was my payout short?"})

    entries, _ = _entries(client)
    asked = [e for e in entries if e.action == Action.ASK_AGENT]
    assert asked
    assert "Why was my payout short?" in asked[0].detail


def test_running_the_auditor_is_recorded(client):
    _owner(client)
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={})

    entries, _ = _entries(client)
    assert any(e.action == Action.RUN_AUDIT and e.target == run_id
               for e in entries)


def test_reading_the_log_is_itself_logged(client):
    """A blind spot at the most sensitive page is where someone would look."""
    _owner(client)
    client.get("/activity")
    client.get("/activity")

    entries, _ = _entries(client)
    assert sum(1 for e in entries if e.action == Action.VIEW_ACCESS_LOG) >= 1


# --- the events that matter more -----------------------------------------

def test_a_refused_settlement_is_recorded(client):
    """Reaching for another business's settlement is exactly what to record."""
    _owner(client, "first@x.in")
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    theirs = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]

    client.get("/logout")
    _owner(client, "second@x.in")
    assert client.get(f"/settlements/{theirs}").status_code == 404

    import merchant.app as appmod
    with appmod.ledger() as led:
        denials = AccessLog(led.conn).denials()
    assert any(d["email"] == "second@x.in"
               and d["action"] == Action.VIEW_SETTLEMENT for d in denials)


def test_holding_a_cookie_for_someone_elses_business_is_recorded(client):
    _owner(client, "first@x.in")

    import merchant.app as appmod
    with appmod.ledger() as led:
        theirs = led.businesses.all()[0]["business_id"]

    client.get("/logout")
    client.post("/signup", data={"email": "outsider@x.in",
                                 "password": "a-good-password"})
    client.cookies.set("business_id", theirs)
    client.get("/")

    with appmod.ledger() as led:
        denials = AccessLog(led.conn).denials()
    assert any(d["email"] == "outsider@x.in"
               and d["action"] == Action.SWITCH_BUSINESS for d in denials)


def test_a_staff_member_attempting_an_owner_action_is_recorded(client):
    _owner(client, "owner@x.in")

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", "a-good-password")
        biz = led.businesses.all()[0]["business_id"]
        auth.add_member(biz, staff.user_id, Role.STAFF)

    client.get("/logout")
    client.post("/login", data={"email": "staff@x.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")
    client.post("/settings/rate", data={"instrument": "credit_card",
                                        "network_pct": "9", "platform_pct": "0"})

    with appmod.ledger() as led:
        denials = AccessLog(led.conn).denials()
    assert any(d["email"] == "staff@x.in"
               and "staff attempted an owner action" in (d["detail"] or "")
               for d in denials)


# --- who may read it -----------------------------------------------------

def test_the_activity_log_is_owner_only(client):
    _owner(client, "owner@x.in")
    assert client.get("/activity").status_code == 200

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@x.in", "a-good-password")
        biz = led.businesses.all()[0]["business_id"]
        auth.add_member(biz, staff.user_id, Role.STAFF)

    client.get("/logout")
    client.post("/login", data={"email": "staff@x.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")
    assert client.get("/activity").status_code == 403


def test_the_operator_sees_refusals_but_not_what_was_read(client):
    """
    An operator investigating an incident needs to know someone tried to reach
    a business they are not in. They do not need, and are not entitled to, what
    that business's settlements say.
    """
    _owner(client, "founder@ledgerline.in")          # first account: operator
    client.post("/sale", data={"rupees": "4321.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.get(f"/settlements/{run_id}")

    page = client.get("/admin").text
    assert "Refused access attempts" in page
    assert "entitled to" in page          # the apostrophe after it is escaped
    # the successful read of a settlement is not surfaced platform-wide
    assert run_id not in page


def test_one_business_cannot_read_anothers_log(client):
    _owner(client, "first@x.in")
    client.get("/activity")

    import merchant.app as appmod
    with appmod.ledger() as led:
        first = led.businesses.all()[0]["business_id"]

    client.get("/logout")
    _owner(client, "second@x.in")
    page = client.get("/activity").text
    assert "first@x.in" not in page
    assert first not in page


def test_a_refusal_records_a_reason_that_is_always_true(client):
    """
    The page does not distinguish "belongs to someone else" from "does not
    exist" - that would let anyone enumerate settlement ids. So the log must
    not claim a reason that might be false: a made-up id is not another
    business's settlement, and recording that it was would be a lie in the one
    place lies matter most.
    """
    _owner(client)
    assert client.get("/settlements/run_completely_made_up").status_code == 404

    import merchant.app as appmod
    with appmod.ledger() as led:
        denials = AccessLog(led.conn).denials()

    entry = next(d for d in denials if d["target"] == "run_completely_made_up")
    assert entry["detail"] == "not reachable from this business"
    assert "another business" not in entry["detail"]
