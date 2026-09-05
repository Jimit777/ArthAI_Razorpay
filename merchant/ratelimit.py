"""
Throttling the login form.

A login with no throttle is an open invitation: passwords are guessable at a
few thousand a second against an endpoint that will happily answer forever.
But the obvious fix introduces two new problems, and both matter more than the
one being solved.

## Problem one: locking out the person being attacked

Counting failures per EMAIL and locking the account is the textbook version and
it is a denial-of-service primitive. Anyone who knows a merchant's email can
lock them out of their own settlements at will, indefinitely, for free.

So nothing here ever locks an account. Limits are per WINDOW - they expire on
their own - and the tighter of the two is keyed on (address, email) rather than
email alone, so an attacker throttles only themselves.

## Problem two: the limiter becomes an oracle

If failures were only counted for accounts that exist, then "you are rate
limited" would mean "that email is registered" and "keep trying" would mean it
is not. The throttle would leak exactly what the login form is careful not to.

So attempts are counted whether or not the account exists, and the response is
identical either way.

## What this is not

Not a defence against a distributed attack - a thousand addresses each get
their own budget. That needs something this prototype does not have, and
pretending otherwise would be worse than the gap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Per address and email: tight, because a real person typing their own password
# wrong five times in a quarter of an hour is already unusual.
PER_IDENTITY_MAX = 5
PER_IDENTITY_WINDOW = 15 * 60

# Per address alone: looser, and it is what catches someone spraying one
# password across many accounts.
PER_ADDRESS_MAX = 20
PER_ADDRESS_WINDOW = 15 * 60

# Signups, to stop an address minting accounts to enumerate emails.
SIGNUP_MAX = 5
SIGNUP_WINDOW = 60 * 60

RATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_attempts (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  address TEXT NOT NULL,
  email   TEXT,
  kind    TEXT NOT NULL,        -- 'login' | 'signup'
  at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_addr ON auth_attempts(address, at);
CREATE INDEX IF NOT EXISTS idx_attempts_pair ON auth_attempts(address, email, at);
"""


@dataclass
class Throttled:
    retry_after: int                    # seconds
    reason: str

    @property
    def human(self) -> str:
        minutes = max(1, round(self.retry_after / 60))
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"


def client_address(request) -> str:
    """
    Who is asking.

    X-Forwarded-For is only trusted when this install says it sits behind a
    proxy. Trusting it unconditionally would let anyone set the header to a
    random value per request and have an unlimited budget - a rate limiter that
    can be bypassed by typing is worse than none, because it looks like one.
    """
    from merchant.vault import env

    # LEDGERLINE_BEHIND_PROXY is still honoured; see merchant.vault.LEGACY_ENV.
    if env("ARTHAI_BEHIND_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return getattr(request.client, "host", "unknown") or "unknown"


class RateLimit:
    """Counts attempts in a sliding window. Nothing is ever locked for good."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(RATE_SCHEMA)
        self.conn.commit()

    # --- checking ---------------------------------------------------------

    def _count(self, since: int, address: str, email: Optional[str],
               kind: str) -> tuple[int, Optional[int]]:
        """How many attempts, and when the oldest one falls out of the window."""
        if email is None:
            row = self.conn.execute(
                "SELECT COUNT(*) n, MIN(at) oldest FROM auth_attempts"
                " WHERE address = ? AND kind = ? AND at > ?",
                (address, kind, since)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) n, MIN(at) oldest FROM auth_attempts"
                " WHERE address = ? AND email = ? AND kind = ? AND at > ?",
                (address, email, kind, since)).fetchone()
        return row["n"], row["oldest"]

    def check_login(self, address: str, email: str) -> Optional[Throttled]:
        now = int(time.time())
        email = (email or "").strip().lower()

        pair_n, pair_oldest = self._count(
            now - PER_IDENTITY_WINDOW, address, email, "login")
        if pair_n >= PER_IDENTITY_MAX:
            return Throttled(pair_oldest + PER_IDENTITY_WINDOW - now,
                             "too many attempts for this account")

        addr_n, addr_oldest = self._count(
            now - PER_ADDRESS_WINDOW, address, None, "login")
        if addr_n >= PER_ADDRESS_MAX:
            return Throttled(addr_oldest + PER_ADDRESS_WINDOW - now,
                             "too many attempts from here")
        return None

    def check_signup(self, address: str) -> Optional[Throttled]:
        now = int(time.time())
        n, oldest = self._count(now - SIGNUP_WINDOW, address, None, "signup")
        if n >= SIGNUP_MAX:
            return Throttled(oldest + SIGNUP_WINDOW - now,
                             "too many accounts created from here")
        return None

    # --- recording --------------------------------------------------------

    def record_failure(self, address: str, email: str, kind: str = "login") -> None:
        """
        Recorded whether or not the account exists.

        Counting only real accounts would turn the throttle into an oracle:
        "you are rate limited" would mean the email is registered.
        """
        self.conn.execute(
            "INSERT INTO auth_attempts (address, email, kind, at)"
            " VALUES (?,?,?,?)",
            (address, (email or "").strip().lower(), kind, int(time.time())))
        self.conn.commit()

    def record_success(self, address: str, email: str) -> None:
        """
        A correct password clears that pair's failures.

        Someone who mistypes three times and then gets it right should not be
        four failures away from being locked out of their own settlements for
        the next quarter of an hour.
        """
        self.conn.execute(
            "DELETE FROM auth_attempts WHERE address = ? AND email = ?"
            " AND kind = 'login'",
            (address, (email or "").strip().lower()))
        self.conn.commit()

    def purge(self, older_than: int = 86_400) -> None:
        self.conn.execute("DELETE FROM auth_attempts WHERE at < ?",
                          (int(time.time()) - older_than,))
        self.conn.commit()
