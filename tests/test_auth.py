"""
Tests for accounts, sessions and roles.

This is the file where a passing test is not enough - a security boundary that
is never attacked is a boundary nobody has checked. So most of what follows
tries to get past something, and asserts it failed.

The role that matters is OWNER, and the reason is specific: every finding in
this product is "you were charged more than your contract says". Whoever can
edit the contract can silently switch findings off. Set UPI network MDR to
0.90% and the auditor stops reporting zero-MDR violations - not because it
broke, but because it was told they are contractual.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.auth import (  # noqa: E402
    MIN_PASSWORD,
    Auth,
    Role,
    hash_password,
    verify_password,
)
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def auth(tmp_path):
    led = Ledger(tmp_path / "auth.db")
    yield Auth(led.conn), led
    led.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


def _signup(client, email, password="a-good-password", name=""):
    return client.post("/signup", data={"email": email, "password": password,
                                        "name": name or email.split("@")[0]})


# --- passwords -----------------------------------------------------------

def test_a_password_is_never_stored(auth):
    a, led = auth
    a.register("meera@boutique.in", "correct-horse-battery")
    row = a.by_email("meera@boutique.in")
    assert "correct-horse-battery" not in str(dict(row))
    assert row["password_hash"] != "correct-horse-battery"
    assert len(row["salt"]) == 32


def test_the_same_password_hashes_differently_for_two_users(auth):
    a, led = auth
    a.register("one@x.in", "identical-password")
    a.register("two@x.in", "identical-password")
    first = a.by_email("one@x.in")
    second = a.by_email("two@x.in")
    assert first["salt"] != second["salt"]
    assert first["password_hash"] != second["password_hash"]


def test_verification_is_constant_time():
    """
    A comparison that returns early leaks how much of the hash matched, one
    byte at a time.
    """
    import inspect

    import merchant.auth as mod

    source = inspect.getsource(mod.verify_password)
    assert "compare_digest" in source
    assert "==" not in source.split("return")[-1]


def test_short_passwords_are_refused(auth):
    a, led = auth
    with pytest.raises(ValueError, match=str(MIN_PASSWORD)):
        a.register("x@y.in", "short")


def test_a_bad_email_is_refused(auth):
    a, led = auth
    for bad in ("not-an-email", "@nope.in", "two@@x.in", ""):
        with pytest.raises(ValueError):
            a.register(bad, "a-good-password")


def test_the_same_email_cannot_register_twice(auth):
    a, led = auth
    a.register("dup@x.in", "a-good-password")
    with pytest.raises(ValueError, match="already exists"):
        a.register("DUP@X.IN", "another-password")


# --- sessions ------------------------------------------------------------

def test_only_the_hash_of_a_session_token_is_stored(auth):
    """
    A copy of the database must not hand anyone a live session, for the same
    reason it does not hand them a password.
    """
    a, led = auth
    user = a.register("meera@boutique.in", "a-good-password")
    token = a.start_session(user.user_id)

    stored = led.conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert stored["token_hash"] != token
    assert token not in str(dict(stored))
    assert a.user_for(token).user_id == user.user_id


def test_an_expired_session_is_not_a_session(auth):
    a, led = auth
    user = a.register("meera@boutique.in", "a-good-password")
    token = a.start_session(user.user_id)
    led.conn.execute("UPDATE sessions SET expires_at = 1")
    led.conn.commit()
    assert a.user_for(token) is None


def test_logging_out_kills_the_session(auth):
    a, led = auth
    user = a.register("meera@boutique.in", "a-good-password")
    token = a.start_session(user.user_id)
    a.logout(token)
    assert a.user_for(token) is None


def test_a_made_up_token_is_nobody(auth):
    a, led = auth
    a.register("meera@boutique.in", "a-good-password")
    assert a.user_for("clearly-not-a-real-token") is None
    assert a.user_for(None) is None
    assert a.user_for("") is None


def test_login_says_nothing_about_which_half_was_wrong(auth):
    """
    Distinguishing "no such account" from "wrong password" tells anyone who
    asks which email addresses are registered here.
    """
    a, led = auth
    a.register("real@x.in", "the-right-password")
    assert a.login("real@x.in", "the-wrong-password") is None
    assert a.login("fake@x.in", "the-right-password") is None
    assert a.login("real@x.in", "the-right-password") is not None


# --- who is the operator -------------------------------------------------

def test_the_first_account_runs_the_platform(auth):
    """Somebody has to reach /admin, and there is nobody to grant it."""
    a, led = auth
    first = a.register("founder@ledgerline.in", "a-good-password")
    second = a.register("merchant@boutique.in", "a-good-password")
    assert first.is_operator
    assert not second.is_operator


def test_an_operator_is_not_automatically_an_owner(auth):
    """
    Running the platform is not the same as being entitled to edit a customer's
    contract. Conflating them is how an operator silently changes what a
    merchant is owed.
    """
    a, led = auth
    op = a.register("founder@ledgerline.in", "a-good-password")
    merchant = a.register("meera@boutique.in", "a-good-password")
    biz = led.businesses.create("Meera's Boutique")
    a.add_member(biz, merchant.user_id, Role.OWNER)

    assert op.is_operator
    assert a.role_in(op, biz) is None
    assert a.role_in(merchant, biz) == Role.OWNER


# --- membership ----------------------------------------------------------

def test_a_user_sees_only_their_own_businesses(auth):
    a, led = auth
    one = a.register("one@x.in", "a-good-password")
    two = a.register("two@x.in", "a-good-password")
    mine = led.businesses.create("Mine")
    theirs = led.businesses.create("Theirs")
    a.add_member(mine, one.user_id, Role.OWNER)
    a.add_member(theirs, two.user_id, Role.OWNER)

    assert [b["name"] for b in a.businesses_for(one)] == ["Mine"]
    assert [b["name"] for b in a.businesses_for(two)] == ["Theirs"]


def test_a_non_member_has_no_role(auth):
    a, led = auth
    outsider = a.register("outsider@x.in", "a-good-password")
    biz = led.businesses.create("Not Theirs")
    assert a.role_in(outsider, biz) is None
    assert a.role_in(None, biz) is None


# --- the web boundary ----------------------------------------------------

def test_every_page_needs_a_login(client):
    for path in ("/", "/agents", "/agents/settlement",
                 "/agents/settlement/ask", "/agents/input-credit",
                 "/settings", "/data",
                 "/data/simulator", "/businesses", "/people", "/admin"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_a_moved_url_still_lands_somewhere_that_needs_a_login(client):
    """
    The old paths are aliases, not pages: they answer 307 and the page they
    point at does the checking. Following one has to end at the login screen,
    or a bookmark from before the move becomes a way in.
    """
    from merchant.nav import MOVED

    for old_path in MOVED:
        if "{" in old_path:
            continue
        response = client.get(old_path, follow_redirects=False)
        assert response.status_code == 307, old_path
        landed = client.get(response.headers["location"],
                            follow_redirects=False)
        assert landed.status_code == 303, old_path
        assert landed.headers["location"] == "/login", old_path


def test_signing_up_signs_you_in(client):
    _signup(client, "meera@boutique.in")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_admin_is_operator_only(client):
    _signup(client, "founder@ledgerline.in")          # first: operator
    assert client.get("/admin").status_code == 200

    client.get("/logout")
    _signup(client, "merchant@boutique.in")           # second: not
    response = client.get("/admin")
    assert response.status_code == 403
    assert "belongs to whoever runs this platform" in response.text


def test_the_admin_link_is_hidden_from_non_operators(client):
    """A control that is visible and then refuses you is worse than absent."""
    _signup(client, "founder@ledgerline.in")
    client.post("/businesses", data={"name": "Ops Co"})
    assert 'href="/admin"' in client.get("/").text

    client.get("/logout")
    _signup(client, "merchant@boutique.in")
    client.post("/businesses", data={"name": "Merchant Co"})
    assert 'href="/admin"' not in client.get("/").text


def test_staff_cannot_edit_the_rate_card(client):
    """
    The boundary that matters. Whoever can edit the contract can silently
    switch findings off.
    """
    _signup(client, "owner@boutique.in")
    client.post("/businesses", data={"name": "Boutique"})
    client.post("/sources/simulator")

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@boutique.in", "a-good-password")
        biz = led.businesses.all()[0]["business_id"]
        auth.add_member(biz, staff.user_id, Role.STAFF)

    client.get("/logout")
    client.post("/login", data={"email": "staff@boutique.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")

    response = client.post("/settings/rate",
                           data={"instrument": "credit_card",
                                 "network_pct": "9.00", "platform_pct": "0"})
    assert response.status_code == 403
    # the apostrophe is HTML-escaped, so match on the part that is not
    assert "signed in as staff" in response.text
    assert "silently switch" in response.text

    # and the rate is untouched
    with appmod.ledger() as led:
        card = led.businesses.rate_card(biz)
    assert card["instruments"]["credit_card"]["network_mdr_bps"] == 200


def test_staff_can_still_do_the_job(client):
    """Locking down the contract must not stop anyone reading the findings."""
    _signup(client, "owner@boutique.in")
    client.post("/businesses", data={"name": "Boutique"})
    client.post("/sources/simulator")
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})
    client.post("/settle")

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@boutique.in", "a-good-password")
        biz = led.businesses.all()[0]["business_id"]
        auth.add_member(biz, staff.user_id, Role.STAFF)

    client.get("/logout")
    client.post("/login", data={"email": "staff@boutique.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")

    for path in ("/", "/settlements", "/ask", "/agents"):
        assert client.get(path).status_code == 200, path


def test_people_is_owner_only(client):
    _signup(client, "owner@boutique.in")
    client.post("/businesses", data={"name": "Boutique"})
    assert client.get("/people").status_code == 200

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        staff = auth.register("staff@boutique.in", "a-good-password")
        biz = led.businesses.all()[0]["business_id"]
        auth.add_member(biz, staff.user_id, Role.STAFF)

    client.get("/logout")
    client.post("/login", data={"email": "staff@boutique.in",
                                "password": "a-good-password"})
    client.get(f"/switch?business_id={biz}")
    assert client.get("/people").status_code == 403


def test_you_cannot_switch_into_someone_elses_business(client):
    """A business id is not a capability."""
    _signup(client, "first@x.in")
    client.post("/businesses", data={"name": "Theirs"})

    import merchant.app as appmod
    with appmod.ledger() as led:
        theirs = led.businesses.all()[0]["business_id"]

    client.get("/logout")
    _signup(client, "second@x.in")
    response = client.get(f"/switch?business_id={theirs}",
                          follow_redirects=False)
    assert response.headers["location"] == "/businesses"
    assert "business_id" not in response.cookies


def test_a_stale_business_cookie_is_not_access(client):
    """Even with the cookie set by hand, membership is what decides."""
    _signup(client, "first@x.in")
    client.post("/businesses", data={"name": "Theirs"})

    import merchant.app as appmod
    with appmod.ledger() as led:
        theirs = led.businesses.all()[0]["business_id"]

    client.get("/logout")
    _signup(client, "second@x.in")
    client.cookies.set("business_id", theirs)

    # no workspace resolves, so the overview falls back to "create one"
    page = client.get("/").text
    assert 'action="/businesses"' in page, "no create-a-business form"
    assert "set up your first business" in page.lower()
    assert "Theirs" not in page, "the other account's business leaked through"
    assert "Theirs" not in page


def test_the_last_owner_cannot_be_removed(client):
    """
    A business with no owner is one nobody can ever correct the rate card of -
    and the rate card is what every finding is measured against.
    """
    _signup(client, "owner@boutique.in")
    client.post("/businesses", data={"name": "Boutique"})

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        biz = led.businesses.all()[0]["business_id"]
        other = auth.register("second@boutique.in", "a-good-password")
        auth.add_member(biz, other.user_id, Role.STAFF)

    response = client.post("/people/remove",
                           data={"user_id": other.user_id},
                           follow_redirects=False)
    assert "ok=" in response.headers["location"]

    with appmod.ledger() as led:
        auth = Auth(led.conn)
        assert auth.owner_count(biz) == 1
        # now try to remove the only owner, via a second owner account
        second = auth.register("boss@boutique.in", "a-good-password")
        auth.add_member(biz, second.user_id, Role.OWNER)
        auth.remove_member(biz, second.user_id)
        assert auth.owner_count(biz) == 1


def test_creating_a_business_makes_you_its_owner(client):
    _signup(client, "meera@boutique.in")
    client.post("/businesses", data={"name": "Meera's Boutique"})

    import merchant.app as appmod
    with appmod.ledger() as led:
        auth = Auth(led.conn)
        biz = led.businesses.all()[0]["business_id"]
        user = auth.by_email("meera@boutique.in")
        assert auth.role_in(auth.by_id(user["user_id"]), biz) == Role.OWNER
