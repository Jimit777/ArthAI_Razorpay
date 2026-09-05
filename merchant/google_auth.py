"""
Sign in with Google - the OAuth 2.0 authorization-code flow, by hand.

Why by hand: this codebase's whole stance is fewest moving parts (CLAUDE.md
s.8), and the flow is four HTTPS calls and some string handling. Authlib
would add a dependency, a middleware and a session backend to save about
sixty lines. The standard library covers it.

WHAT IS AND IS NOT VERIFIED HERE
--------------------------------
The ID token that comes back is a JWT signed by Google. This module does
NOT check that RS256 signature, and that is deliberate rather than an
omission: the token is fetched by THIS server, over TLS, directly from
Google's token endpoint, in a back-channel POST the browser never touches.
TLS already proves who sent it. Google's own documentation says so - "you
can skip verifying the ID token" when it comes straight from the token
endpoint over HTTPS.

That reasoning holds only for tokens obtained the way exchange_code() gets
them. It would NOT hold for a token handed to us by a browser (the implicit
flow, or a client-side "here is my token" endpoint), because then anyone
could mint one. If this module ever grows a path that accepts a token from
the front end, that path must verify the signature against Google's JWKS.

What IS still checked, because TLS does not establish any of it:
  - `aud` is our own client ID - a token minted for a different app, even a
    genuine Google one, is not a login here
  - `iss` is Google
  - `exp` has not passed
  - `email_verified` is true - an unverified address must never be allowed
    to claim an existing account (see Auth.upsert_google_user)
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# The state cookie is short-lived: it only has to survive the round trip to
# Google's consent screen and back.
STATE_COOKIE = "arthai_oauth_state"
STATE_MAX_AGE = 600

ENV_CLIENT_ID = "GOOGLE_CLIENT_ID"
ENV_CLIENT_SECRET = "GOOGLE_CLIENT_SECRET"
ENV_REDIRECT_URI = "GOOGLE_REDIRECT_URI"


class GoogleAuthError(Exception):
    """Anything that went wrong in the flow. Message is safe to show."""


def client_id() -> str:
    return (os.environ.get(ENV_CLIENT_ID) or "").strip()


def client_secret() -> str:
    return (os.environ.get(ENV_CLIENT_SECRET) or "").strip()


def is_configured() -> bool:
    """
    Whether to offer Google sign-in at all.

    The button is hidden rather than shown-and-broken when the deployment
    has no credentials: a control that cannot work is worse than no control.
    """
    return bool(client_id() and client_secret())


def redirect_uri(request_base_url: str = "") -> str:
    """
    Where Google sends the browser back to.

    Google matches this string EXACTLY against the Authorised redirect URI
    registered in the Cloud console, so an explicit env var wins - deriving
    it from the request breaks the moment the app is behind a proxy that
    rewrites scheme or host.
    """
    explicit = (os.environ.get(ENV_REDIRECT_URI) or "").strip()
    if explicit:
        return explicit
    return urllib.parse.urljoin(request_base_url or "http://127.0.0.1:8000/",
                                "auth/google/callback")


def authorize_url(state: str, base_url: str = "") -> str:
    """The consent screen to send the browser to."""
    if not is_configured():
        raise GoogleAuthError("Google sign-in is not configured on this server.")
    query = urllib.parse.urlencode({
        "client_id": client_id(),
        "redirect_uri": redirect_uri(base_url),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # Ask for an account chooser rather than silently reusing whichever
        # Google account the browser happens to be signed into.
        "prompt": "select_account",
    })
    return f"{AUTH_ENDPOINT}?{query}"


def _decode_segment(segment: str) -> dict:
    """base64url-decode one JWT segment, restoring the stripped padding."""
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def _claims_from_id_token(id_token: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise GoogleAuthError("Google returned a malformed ID token.")
    try:
        return _decode_segment(parts[1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleAuthError("Google returned an unreadable ID token.") from exc


def validate_claims(claims: dict, now: Optional[int] = None) -> dict:
    """
    Check everything TLS does not establish. See this module's docstring
    for why the signature itself is not re-checked here.

    Returns {"sub", "email", "name"} on success; raises otherwise.
    """
    now = int(time.time()) if now is None else now

    if claims.get("aud") != client_id():
        # A perfectly genuine Google token minted for somebody else's app is
        # not a login here. Skipping this is the classic OAuth hole.
        raise GoogleAuthError("That Google token was issued for another app.")

    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("That token did not come from Google.")

    try:
        expires_at = int(claims.get("exp", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= now:
        raise GoogleAuthError("That Google sign-in expired. Try again.")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Google did not return an email address.")

    # email_verified arrives as a real bool from Google, but has historically
    # been a string in some responses - accept both, reject everything else.
    verified = claims.get("email_verified")
    if verified not in (True, "true"):
        raise GoogleAuthError(
            "That Google account's email address is not verified.")

    subject = (claims.get("sub") or "").strip()
    if not subject:
        raise GoogleAuthError("Google did not return an account identifier.")

    return {"sub": subject, "email": email,
            "name": (claims.get("name") or "").strip()}


def exchange_code(code: str, base_url: str = "", *, timeout: float = 10.0) -> dict:
    """
    Trade the one-time code for tokens, back-channel, and return the
    validated {"sub", "email", "name"}.

    This is a blocking call. Every route in this app is a sync `def`, which
    FastAPI already runs in a worker thread, so it does not block the loop.
    """
    if not is_configured():
        raise GoogleAuthError("Google sign-in is not configured on this server.")

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "redirect_uri": redirect_uri(base_url),
        "grant_type": "authorization_code",
    }).encode()

    request = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Google's error body names the cause (redirect_uri_mismatch is the
        # usual one); it is safe to surface and saves a lot of guessing.
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error", "")
        except Exception:
            pass
        raise GoogleAuthError(
            f"Google rejected the sign-in{f' ({detail})' if detail else ''}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GoogleAuthError("Could not reach Google to finish signing in.") from exc

    id_token = payload.get("id_token")
    if not id_token:
        raise GoogleAuthError("Google did not return an ID token.")

    return validate_claims(_claims_from_id_token(id_token))
