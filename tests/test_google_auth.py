"""
Tests for Sign in with Google.

The interesting surface here is not the happy path - it is everything the
flow must REFUSE. A token minted for another app, an unverified email
address, a stale sign-in, a callback nobody started: each of those is a way
in if it is not checked, so each gets a test.
"""

import base64
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from merchant import google_auth  # noqa: E402
from merchant.auth import Auth  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402

CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A live app pointed at a throwaway database - same shape as the one
    in test_merchant.py, which is module-local rather than in a conftest."""
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")
    return CLIENT_ID


def _claims(**over) -> dict:
    base = {
        "aud": CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": int(time.time()) + 600,
        "sub": "1234567890",
        "email": "meera@example.com",
        "email_verified": True,
        "name": "Meera",
    }
    base.update(over)
    return base


# --- configuration ---------------------------------------------------------

def test_google_signin_is_off_until_both_secrets_are_present(monkeypatch):
    """A half-configured server must not offer a button that cannot work."""
    monkeypatch.delenv(google_auth.ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(google_auth.ENV_CLIENT_SECRET, raising=False)
    assert not google_auth.is_configured()

    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    assert not google_auth.is_configured(), "client id alone is not enough"

    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "s")
    assert google_auth.is_configured()


def test_the_consent_url_carries_the_state_and_asks_for_an_account(configured):
    url = google_auth.authorize_url("st4te", "http://127.0.0.1:8000/")
    assert url.startswith(google_auth.AUTH_ENDPOINT)
    assert "state=st4te" in url
    assert "response_type=code" in url
    assert "prompt=select_account" in url
    assert "scope=openid+email+profile" in url


def test_an_explicit_redirect_uri_wins_over_the_request(configured, monkeypatch):
    """Google matches this string exactly, so a proxy rewriting the host
    must not be allowed to change it."""
    monkeypatch.setenv(google_auth.ENV_REDIRECT_URI,
                       "https://artha.example/auth/google/callback")
    assert google_auth.redirect_uri("http://127.0.0.1:8000/") == \
        "https://artha.example/auth/google/callback"


# --- what validate_claims must refuse --------------------------------------

def test_a_token_minted_for_another_app_is_refused(configured):
    """The classic OAuth hole: a real Google token for somebody else's
    client id is not a login here."""
    with pytest.raises(google_auth.GoogleAuthError, match="another app"):
        google_auth.validate_claims(_claims(aud="someone-else.apps.googleusercontent.com"))


def test_a_token_from_the_wrong_issuer_is_refused(configured):
    with pytest.raises(google_auth.GoogleAuthError, match="did not come from Google"):
        google_auth.validate_claims(_claims(iss="https://evil.example"))


def test_an_expired_sign_in_is_refused(configured):
    with pytest.raises(google_auth.GoogleAuthError, match="expired"):
        google_auth.validate_claims(_claims(exp=int(time.time()) - 1))


def test_an_unverified_email_is_refused(configured):
    """This one guards account linking: upsert_google_user will attach a
    Google identity to an existing password account on email alone, which is
    only safe because an unverified address never gets this far."""
    with pytest.raises(google_auth.GoogleAuthError, match="not verified"):
        google_auth.validate_claims(_claims(email_verified=False))


def test_a_verified_claim_set_comes_back_normalised(configured):
    out = google_auth.validate_claims(_claims(email="Meera@Example.COM "))
    assert out == {"sub": "1234567890", "email": "meera@example.com",
                   "name": "Meera"}


def test_the_string_form_of_email_verified_is_accepted(configured):
    assert google_auth.validate_claims(_claims(email_verified="true"))["sub"]


# --- the account behind the identity ---------------------------------------

def test_signing_in_twice_reuses_the_same_account(tmp_path):
    led = Ledger(tmp_path / "g.db")
    auth = Auth(led.conn)

    first = auth.upsert_google_user("sub-1", "meera@example.com", "Meera")
    again = auth.upsert_google_user("sub-1", "meera@example.com", "Meera")

    assert first.user_id == again.user_id
    led.close()


def test_a_changed_email_still_finds_the_account_by_its_google_id(tmp_path):
    """`sub` is Google's stable identifier; the email address on an account
    is not. Matching on email alone would have created a second account."""
    led = Ledger(tmp_path / "g.db")
    auth = Auth(led.conn)

    first = auth.upsert_google_user("sub-1", "meera@example.com")
    moved = auth.upsert_google_user("sub-1", "meera@newdomain.com")

    assert first.user_id == moved.user_id
    led.close()


def test_google_links_to_an_existing_password_account_rather_than_duplicating(tmp_path):
    led = Ledger(tmp_path / "g.db")
    auth = Auth(led.conn)
    existing = auth.register("meera@example.com", "a-long-password", "Meera")

    linked = auth.upsert_google_user("sub-1", "meera@example.com", "Meera")

    assert linked.user_id == existing.user_id, "a second account was created"
    assert led.conn.execute(
        "SELECT COUNT(*) c FROM users").fetchone()["c"] == 1
    led.close()


def test_a_google_account_cannot_be_signed_into_with_a_password(tmp_path):
    """
    There is no password to guess, and login() has no special case for
    Google accounts - it fails closed because the stored hash is random
    bytes no scrypt digest will match.
    """
    led = Ledger(tmp_path / "g.db")
    auth = Auth(led.conn)
    auth.upsert_google_user("sub-1", "meera@example.com", "Meera")

    assert auth.login("meera@example.com", "") is None
    assert auth.login("meera@example.com", "password") is None
    led.close()


def test_the_first_google_account_on_a_fresh_install_runs_the_platform(tmp_path):
    """Same rule register() applies - somebody has to be able to reach
    /admin, and arriving via Google does not change that."""
    led = Ledger(tmp_path / "g.db")
    first = Auth(led.conn).upsert_google_user("sub-1", "first@example.com")
    second = Auth(led.conn).upsert_google_user("sub-2", "second@example.com")

    assert first.is_operator
    assert not second.is_operator
    led.close()


def test_an_identity_missing_its_pieces_is_rejected(tmp_path):
    led = Ledger(tmp_path / "g.db")
    auth = Auth(led.conn)
    with pytest.raises(ValueError):
        auth.upsert_google_user("", "meera@example.com")
    with pytest.raises(ValueError):
        auth.upsert_google_user("sub-1", "")
    led.close()


# --- the routes -------------------------------------------------------------

def test_the_button_is_hidden_when_google_is_not_configured(client, monkeypatch):
    monkeypatch.delenv(google_auth.ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(google_auth.ENV_CLIENT_SECRET, raising=False)
    # An account must exist or /login redirects to /signup.
    client.post("/signup", data={"name": "A", "email": "a@example.com",
                                 "password": "a-long-password"})
    client.get("/logout")

    page = client.get("/login").text
    assert "/login/google" not in page


def test_the_button_appears_once_google_is_configured(client, monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")
    client.post("/signup", data={"name": "A", "email": "a@example.com",
                                 "password": "a-long-password"})
    client.get("/logout")

    page = client.get("/login").text
    assert 'href="/login/google"' in page
    assert "Continue with Google" in page


def test_starting_the_flow_sets_a_state_cookie_and_redirects_to_google(
        client, monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")

    response = client.get("/login/google", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(google_auth.AUTH_ENDPOINT)
    assert google_auth.STATE_COOKIE in response.cookies


def test_a_callback_with_no_state_cookie_is_refused(client, monkeypatch):
    """A callback the browser did not start - the CSRF case."""
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")

    response = client.get("/auth/google/callback?code=abc&state=whatever",
                          follow_redirects=False)

    assert response.status_code == 303
    assert "/login?error=" in response.headers["location"]
    assert "did%20not%20come%20from%20here" in response.headers["location"]


def test_a_callback_whose_state_does_not_match_is_refused(client, monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")
    client.get("/login/google", follow_redirects=False)

    response = client.get("/auth/google/callback?code=abc&state=not-the-one",
                          follow_redirects=False)

    assert "did%20not%20come%20from%20here" in response.headers["location"]


def test_pressing_cancel_on_google_is_not_reported_as_a_failure(
        client, monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")

    response = client.get("/auth/google/callback?error=access_denied",
                          follow_redirects=False)

    assert "cancelled" in response.headers["location"]


def test_a_completed_google_sign_in_creates_a_session(client, monkeypatch):
    """The one happy-path test: the token exchange is stubbed (it is a call
    to Google), everything after it is real."""
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")
    monkeypatch.setattr(
        "merchant.app.google_auth.exchange_code",
        lambda code, base_url="": {"sub": "sub-1", "email": "meera@example.com",
                                   "name": "Meera"})

    started = client.get("/login/google", follow_redirects=False)
    state = started.cookies[google_auth.STATE_COOKIE]

    response = client.get(f"/auth/google/callback?code=abc&state={state}",
                          follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_a_failed_exchange_does_not_sign_anyone_in(client, monkeypatch):
    monkeypatch.setenv(google_auth.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(google_auth.ENV_CLIENT_SECRET, "test-secret")

    def boom(code, base_url=""):
        raise google_auth.GoogleAuthError("Google rejected the sign-in.")

    monkeypatch.setattr("merchant.app.google_auth.exchange_code", boom)

    started = client.get("/login/google", follow_redirects=False)
    state = started.cookies[google_auth.STATE_COOKIE]
    response = client.get(f"/auth/google/callback?code=abc&state={state}",
                          follow_redirects=False)

    assert "/login?error=" in response.headers["location"]


# --- the ID token itself ----------------------------------------------------

def test_claims_are_read_out_of_a_real_shaped_jwt(configured):
    """The payload segment is base64url with its padding stripped - the
    decoder has to put it back."""
    payload = base64.urlsafe_b64encode(
        json.dumps(_claims()).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"

    assert google_auth._claims_from_id_token(token)["sub"] == "1234567890"


def test_a_malformed_id_token_is_refused(configured):
    with pytest.raises(google_auth.GoogleAuthError, match="malformed"):
        google_auth._claims_from_id_token("not-a-jwt")
