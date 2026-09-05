"""
Tests for login throttling.

A rate limiter is easy to write and easy to write badly, and the two obvious
mistakes are both worse than the problem they solve:

  locking the ACCOUNT turns the limiter into a denial-of-service primitive -
  anyone who knows a merchant's email can lock them out of their own
  settlements, indefinitely, for free

  counting only real accounts turns it into an oracle - "you are rate limited"
  becomes "that email is registered", leaking exactly what the login form is
  careful not to

Most of what follows checks that neither happened.
"""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.auth import Auth  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402
from merchant.ratelimit import (  # noqa: E402
    PER_ADDRESS_MAX,
    PER_IDENTITY_MAX,
    SIGNUP_MAX,
    RateLimit,
    client_address,
)


@pytest.fixture
def limiter(tmp_path):
    led = Ledger(tmp_path / "rl.db")
    yield RateLimit(led.conn), led
    led.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


# --- the basic budget ----------------------------------------------------

def test_attempts_are_allowed_up_to_the_limit(limiter):
    rl, _ = limiter
    for _ in range(PER_IDENTITY_MAX):
        assert rl.check_login("1.1.1.1", "meera@x.in") is None
        rl.record_failure("1.1.1.1", "meera@x.in")
    assert rl.check_login("1.1.1.1", "meera@x.in") is not None


def test_being_throttled_says_how_long(limiter):
    rl, _ = limiter
    for _ in range(PER_IDENTITY_MAX):
        rl.record_failure("1.1.1.1", "meera@x.in")
    throttled = rl.check_login("1.1.1.1", "meera@x.in")
    assert throttled.retry_after > 0
    assert "minute" in throttled.human


def test_spraying_one_password_across_accounts_is_caught(limiter):
    """
    The per-address budget. Each email individually stays under its own limit,
    which is exactly how a spraying attack stays invisible to a per-account one.
    """
    rl, _ = limiter
    for i in range(PER_ADDRESS_MAX):
        rl.record_failure("9.9.9.9", f"victim{i}@x.in")
    assert rl.check_login("9.9.9.9", "someone-else@x.in") is not None


# --- the mistake that locks out the victim -------------------------------

def test_an_attacker_cannot_lock_someone_out_of_their_own_account(limiter):
    """
    THE failure mode. Counting per email alone means anyone who knows a
    merchant's address can lock them out of their settlements at will.

    The tight limit is keyed on (address, email), so an attacker exhausts only
    their own budget - the real owner, from their own address, is unaffected.
    """
    rl, _ = limiter
    attacker, owner = "6.6.6.6", "10.0.0.5"

    for _ in range(PER_IDENTITY_MAX * 3):
        rl.record_failure(attacker, "meera@boutique.in")

    assert rl.check_login(attacker, "meera@boutique.in") is not None
    assert rl.check_login(owner, "meera@boutique.in") is None, (
        "the owner was locked out of their own account")


def test_nothing_is_ever_locked_permanently(limiter):
    """
    Limits expire on their own. A permanent lock needs an unlock, and an
    unlock nobody can perform is an outage.
    """
    rl, led = limiter
    for _ in range(PER_IDENTITY_MAX):
        rl.record_failure("1.1.1.1", "meera@x.in")
    assert rl.check_login("1.1.1.1", "meera@x.in") is not None

    # age the attempts past the window
    led.conn.execute("UPDATE auth_attempts SET at = at - 100000")
    led.conn.commit()
    assert rl.check_login("1.1.1.1", "meera@x.in") is None


def test_getting_it_right_clears_the_failures(limiter):
    """
    Someone who mistypes and then succeeds should not be one slip away from a
    lockout for the next quarter of an hour.
    """
    rl, _ = limiter
    for _ in range(PER_IDENTITY_MAX - 1):
        rl.record_failure("1.1.1.1", "meera@x.in")
    rl.record_success("1.1.1.1", "meera@x.in")

    for _ in range(PER_IDENTITY_MAX - 1):
        assert rl.check_login("1.1.1.1", "meera@x.in") is None
        rl.record_failure("1.1.1.1", "meera@x.in")


# --- the mistake that leaks which emails exist ---------------------------

def test_unknown_emails_are_counted_too(limiter):
    """
    If only real accounts were counted, "you are rate limited" would mean "that
    email is registered" - the throttle would leak what the login form hides.
    """
    rl, _ = limiter
    for _ in range(PER_IDENTITY_MAX):
        rl.record_failure("1.1.1.1", "definitely-not-a-user@nowhere.in")
    assert rl.check_login("1.1.1.1", "definitely-not-a-user@nowhere.in") is not None


def test_the_throttled_response_is_the_same_for_real_and_fake_accounts(client):
    client.post("/signup", data={"email": "real@x.in", "password": "a-good-password"})
    client.get("/logout")

    def exhaust(email):
        last = None
        for _ in range(PER_IDENTITY_MAX + 1):
            last = client.post("/login", data={"email": email,
                                               "password": "wrong-password"},
                               follow_redirects=False)
        return last.headers["location"]

    assert "Too+many+attempts" in exhaust("real@x.in").replace("%20", "+")
    assert "Too+many+attempts" in exhaust("fake@x.in").replace("%20", "+")


# --- through the web -----------------------------------------------------

def test_the_login_form_throttles(client):
    client.post("/signup", data={"email": "meera@x.in",
                                 "password": "a-good-password"})
    client.get("/logout")

    for _ in range(PER_IDENTITY_MAX):
        response = client.post("/login", data={"email": "meera@x.in",
                                               "password": "wrong"},
                               follow_redirects=False)
        assert "wrong" in response.headers["location"].lower()

    blocked = client.post("/login", data={"email": "meera@x.in",
                                          "password": "wrong"},
                          follow_redirects=False)
    assert "Too+many" in blocked.headers["location"].replace("%20", "+")

    # and the CORRECT password is refused too while throttled - otherwise the
    # limit would only slow down someone who was already going to fail
    still = client.post("/login", data={"email": "meera@x.in",
                                        "password": "a-good-password"},
                        follow_redirects=False)
    assert "Too+many" in still.headers["location"].replace("%20", "+")


def test_signups_are_throttled(client):
    for i in range(SIGNUP_MAX):
        client.post("/signup", data={"email": f"user{i}@x.in",
                                     "password": "a-good-password"})
        client.get("/logout")

    blocked = client.post("/signup", data={"email": "one-too-many@x.in",
                                           "password": "a-good-password"},
                          follow_redirects=False)
    assert "Too+many+accounts" in blocked.headers["location"].replace("%20", "+")


def test_a_failed_signup_still_counts(client):
    """Otherwise the signup form is a free email-enumeration oracle."""
    for i in range(SIGNUP_MAX):
        client.post("/signup", data={"email": "not-an-email", "password": "short"},
                    follow_redirects=False)

    blocked = client.post("/signup", data={"email": "fine@x.in",
                                           "password": "a-good-password"},
                          follow_redirects=False)
    assert "Too+many+accounts" in blocked.headers["location"].replace("%20", "+")


# --- who is asking -------------------------------------------------------

def test_a_forwarded_header_is_ignored_unless_this_install_is_behind_a_proxy(monkeypatch):
    """
    Trusting X-Forwarded-For unconditionally lets anyone set it per request and
    have an unlimited budget. A limiter that can be bypassed by typing is worse
    than none, because it looks like one.
    """
    class _Request:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.9"})()

    monkeypatch.delenv("ARTHAI_BEHIND_PROXY", raising=False)
    assert client_address(_Request()) == "10.0.0.9"

    monkeypatch.setenv("ARTHAI_BEHIND_PROXY", "1")
    assert client_address(_Request()) == "1.2.3.4"


def test_a_spoofed_header_cannot_reset_the_budget(client):
    """The web-level version of the same guarantee."""
    client.post("/signup", data={"email": "meera@x.in",
                                 "password": "a-good-password"})
    client.get("/logout")

    for i in range(PER_IDENTITY_MAX):
        client.post("/login", data={"email": "meera@x.in", "password": "wrong"},
                    headers={"X-Forwarded-For": f"5.5.5.{i}"},
                    follow_redirects=False)

    blocked = client.post("/login", data={"email": "meera@x.in",
                                          "password": "wrong"},
                          headers={"X-Forwarded-For": "5.5.5.99"},
                          follow_redirects=False)
    assert "Too+many" in blocked.headers["location"].replace("%20", "+")
