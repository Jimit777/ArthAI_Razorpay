"""
Tests for promoting and demoting operators.

The interesting requirement is not that the button works - it is the one move
the button refuses. An operator who can demote themselves can, if they are the
last one, lock every future operator out of /admin. There is no route back from
that state: the only repair is editing the database by hand. So self-demotion
is refused, and because every other demotion is performed by somebody who
remains an operator afterwards, that single refusal is enough to guarantee the
platform always has at least one.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.accesslog import Action  # noqa: E402
from merchant.auth import Auth  # noqa: E402

PASSWORD = "a-good-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def two_accounts(client):
    """The first account is the operator; the second is an ordinary user."""
    client.post("/signup", data={"email": "op@x.in", "password": PASSWORD})
    client.post("/signup", data={"email": "staff@x.in", "password": PASSWORD})
    client.post("/login", data={"email": "op@x.in", "password": PASSWORD})
    return client


def _user(email):
    import merchant.app as appmod

    with appmod.ledger() as led:
        return Auth(led.conn).by_email(email)


def _role_log():
    import merchant.app as appmod

    with appmod.ledger() as led:
        return led.conn.execute(
            "SELECT * FROM access_log WHERE action = ? ORDER BY at",
            (str(Action.CHANGE_ROLE),)).fetchall()


def _promote(client, email, make="operator"):
    return client.post("/admin/role",
                       data={"user_id": _user(email)["user_id"], "make": make},
                       follow_redirects=False)


# --- the flag actually moves ---------------------------------------------

def test_first_account_is_the_operator_and_the_second_is_not(two_accounts):
    assert _user("op@x.in")["is_operator"] == 1
    assert _user("staff@x.in")["is_operator"] == 0


def test_operator_can_promote_somebody(two_accounts):
    _promote(two_accounts, "staff@x.in")
    assert _user("staff@x.in")["is_operator"] == 1


def test_operator_can_demote_somebody_else(two_accounts):
    _promote(two_accounts, "staff@x.in")
    _promote(two_accounts, "staff@x.in", make="user")
    assert _user("staff@x.in")["is_operator"] == 0


def test_promotion_takes_effect_without_signing_in_again(two_accounts):
    """
    is_operator is read from the table on every request rather than baked into
    the session, so a promoted user does not have to log out and back in.
    """
    _promote(two_accounts, "staff@x.in")
    two_accounts.post("/login", data={"email": "staff@x.in", "password": PASSWORD})
    assert two_accounts.get("/admin").status_code == 200


# --- the refusal that keeps the platform reachable ------------------------

def test_operator_cannot_demote_themselves(two_accounts):
    _promote(two_accounts, "op@x.in", make="user")
    assert _user("op@x.in")["is_operator"] == 1


def test_the_platform_can_never_run_out_of_operators(two_accounts):
    """
    Every demotion is performed by an operator who stays one, so no sequence of
    button presses reaches zero.
    """
    _promote(two_accounts, "staff@x.in")
    for email in ("op@x.in", "staff@x.in", "op@x.in"):
        _promote(two_accounts, email, make="user")
        with_flag = [u for u in _all_users() if u["is_operator"]]
        assert with_flag, "the platform lost its last operator"


def _all_users():
    import merchant.app as appmod

    with appmod.ledger() as led:
        return Auth(led.conn).users()


def test_self_demotion_offers_no_button(two_accounts):
    """The refusal is enforced server-side, but the UI should not dangle it."""
    page = two_accounts.get("/admin").text
    row = page.split("op@x.in")[1].split("</tr>")[0]
    assert "demote" not in row


# --- only operators, and only real accounts ------------------------------

def test_an_ordinary_user_cannot_promote_anybody(two_accounts):
    two_accounts.post("/login", data={"email": "staff@x.in", "password": PASSWORD})
    _promote(two_accounts, "staff@x.in")
    assert _user("staff@x.in")["is_operator"] == 0


def test_a_signed_out_visitor_cannot_promote_anybody(client):
    client.post("/signup", data={"email": "op@x.in", "password": PASSWORD})
    client.post("/signup", data={"email": "staff@x.in", "password": PASSWORD})
    client.get("/logout")
    _promote(client, "staff@x.in")
    assert _user("staff@x.in")["is_operator"] == 0


def test_an_unknown_account_is_refused_not_created(two_accounts):
    before = len(_all_users())
    two_accounts.post("/admin/role",
                      data={"user_id": "usr_nonexistent", "make": "operator"})
    assert len(_all_users()) == before


# --- and it is recorded --------------------------------------------------

def test_a_promotion_is_written_to_the_access_log(two_accounts):
    _promote(two_accounts, "staff@x.in")
    entries = _role_log()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "allowed"
    assert entries[0]["email"] == "op@x.in"
    assert "staff@x.in" in entries[0]["detail"]
    assert "promoted" in entries[0]["detail"]


def test_a_refused_promotion_is_recorded_as_a_refusal(two_accounts):
    _promote(two_accounts, "op@x.in", make="user")
    entries = _role_log()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "denied"


def test_the_log_says_which_direction_the_change_went(two_accounts):
    _promote(two_accounts, "staff@x.in")
    _promote(two_accounts, "staff@x.in", make="user")
    details = [e["detail"] for e in _role_log()]
    assert "promoted to operator" in details[0]
    assert "returned to ordinary user" in details[1]


def test_a_refusal_renders_in_the_operators_own_denials_table(two_accounts):
    """
    /admin lists refusals and looks each action up by label. A new Action with
    no label would break the very page the button lives on.
    """
    from merchant.accesslog import ACTION_LABEL

    assert Action.CHANGE_ROLE in ACTION_LABEL
    _promote(two_accounts, "op@x.in", make="user")
    page = two_accounts.get("/admin")
    assert page.status_code == 200
    assert ACTION_LABEL[Action.CHANGE_ROLE] in page.text


def test_the_operator_is_told_what_happened(two_accounts):
    page = _promote(two_accounts, "staff@x.in").headers["location"]
    assert page.startswith("/admin?ok=")
    assert "staff%40x.in" in page
