"""
Accounts, sessions and who is allowed to do what.

Until now this was one undifferentiated surface: a merchant's rate card, the
platform's agent catalogue, and a switch that makes the gateway misbehave all
sat on the same settings page. Three audiences, one screen, no boundary.

## The three roles, and why each exists

  OPERATOR   runs the platform. Sees every business, decides which agents are
             live, and is the only role that can reach /admin. This is you,
             not your customers.

  OWNER      runs one business. Owns the RATE CARD, which is the thing that
             matters: every finding in this product is "you were charged more
             than your contract says", so whoever can edit the contract can
             silently switch findings off. Set UPI network MDR to 0.90% and
             the auditor stops reporting zero-MDR violations - not because it
             broke, but because it was told they are contractual.

  STAFF      works in one business. Reads findings, asks questions, runs
             audits. Cannot touch the contract, the thresholds or the data
             source connection.

## Passwords

scrypt from the standard library, per-user salt, constant-time comparison. No
new dependency, and nothing here invents its own cryptography beyond choosing
parameters - the parameters below are the ones Python's own documentation
recommends for interactive logins.

## Sessions

A random token in a cookie; only its HASH is stored. A copy of the database
therefore does not hand anyone a live session, which is the same reason the
password is not stored either.

## What this still does not make safe

Auth was necessary for live Razorpay credentials. It is not sufficient. A
secret would still sit in plaintext at rest in a SQLite file that anyone with
the disk can read, so the connector continues to refuse live keys. The
remaining blocker is encryption at rest with a key held outside the database -
not login.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

SESSION_COOKIE = "session"
SESSION_DAYS = 30

# Interactive-login parameters from the CPython hashlib documentation.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 64

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


class Role(StrEnum):
    OWNER = "owner"
    STAFF = "staff"


ROLE_LABEL = {
    Role.OWNER: "Owner",
    Role.STAFF: "Staff",
}

ROLE_BLURB = {
    Role.OWNER: "Can change the rate card, the review thresholds and the data "
                "source connection.",
    Role.STAFF: "Can read findings, ask questions and run audits. Cannot change "
                "the contract everything is checked against.",
}

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id       TEXT PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  password_hash TEXT NOT NULL,
  salt          TEXT NOT NULL,
  is_operator   INTEGER DEFAULT 0,
  created_at    INTEGER
);

-- Only the HASH of a session token is stored, for the same reason the password
-- is not: a copy of this file must not hand anyone a live session.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  created_at INTEGER,
  expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS memberships (
  user_id     TEXT,
  business_id TEXT,
  role        TEXT NOT NULL,
  added_at    INTEGER,
  PRIMARY KEY (user_id, business_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


@dataclass
class User:
    user_id: str
    email: str
    name: str
    is_operator: bool


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
    return digest.hex(), salt


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    # Constant time: a comparison that returns early leaks how much of the
    # hash matched, one byte at a time.
    return hmac.compare_digest(candidate, expected_hash)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    """Users, sessions and memberships. Wraps an open connection."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(AUTH_SCHEMA)
        self.conn.commit()

    # --- accounts ---------------------------------------------------------

    def any_users(self) -> bool:
        return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def register(self, email: str, password: str, name: str = "",
                 is_operator: Optional[bool] = None) -> User:
        """
        Create an account.

        The FIRST account on a fresh install becomes the operator, because
        somebody has to be able to reach /admin and there is nobody to grant it.
        Every account after that is an ordinary user until an operator says
        otherwise.
        """
        email = (email or "").strip().lower()
        if not EMAIL.match(email):
            raise ValueError("that does not look like an email address")
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if self.by_email(email):
            raise ValueError("an account with that email already exists")

        if is_operator is None:
            is_operator = not self.any_users()

        digest, salt = hash_password(password)
        user_id = f"usr_{secrets.token_hex(6)}"
        self.conn.execute(
            "INSERT INTO users (user_id, email, name, password_hash, salt,"
            " is_operator, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, email, name.strip() or email.split("@")[0], digest, salt,
             int(is_operator), int(time.time())))
        self.conn.commit()
        return User(user_id, email, name.strip() or email.split("@")[0],
                    bool(is_operator))

    def by_email(self, email: str):
        return self.conn.execute(
            "SELECT * FROM users WHERE email = ?",
            ((email or "").strip().lower(),)).fetchone()

    def by_id(self, user_id: str) -> Optional[User]:
        row = self.conn.execute("SELECT * FROM users WHERE user_id = ?",
                                (user_id,)).fetchone()
        return self._user(row)

    @staticmethod
    def _user(row) -> Optional[User]:
        if row is None:
            return None
        return User(row["user_id"], row["email"], row["name"],
                    bool(row["is_operator"]))

    def set_operator(self, user_id: str, is_operator: bool) -> None:
        """
        Grant or withdraw the operator flag.

        Callers are responsible for refusing to strand the platform - see
        /admin/role, which will not let an operator demote themselves.
        """
        self.conn.execute("UPDATE users SET is_operator = ? WHERE user_id = ?",
                          (int(is_operator), user_id))
        self.conn.commit()

    def operator_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM users WHERE is_operator = 1").fetchone()["n"]

    def users(self) -> list:
        return self.conn.execute(
            "SELECT u.*, ("
            "  SELECT COUNT(*) FROM memberships m WHERE m.user_id = u.user_id"
            " ) AS businesses FROM users u ORDER BY created_at").fetchall()

    # --- sessions ---------------------------------------------------------

    def login(self, email: str, password: str) -> Optional[str]:
        """Returns a session token, or None. Deliberately says nothing about why."""
        row = self.by_email(email)
        if row is None:
            # Hash anyway. Returning instantly for an unknown address tells an
            # attacker which addresses exist.
            hash_password(password or "x")
            return None
        if not verify_password(password or "", row["password_hash"], row["salt"]):
            return None
        return self.start_session(row["user_id"])

    def start_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
            " VALUES (?,?,?,?)",
            (_token_hash(token), user_id, now, now + SESSION_DAYS * 86_400))
        self.conn.commit()
        return token

    def user_for(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires_at > ?",
            (_token_hash(token), int(time.time()))).fetchone()
        return self._user(row)

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                              (_token_hash(token),))
            self.conn.commit()

    def purge_expired(self) -> None:
        self.conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                          (int(time.time()),))
        self.conn.commit()

    # --- membership -------------------------------------------------------

    def add_member(self, business_id: str, user_id: str,
                   role: Role = Role.STAFF) -> None:
        self.conn.execute(
            "INSERT INTO memberships (user_id, business_id, role, added_at)"
            " VALUES (?,?,?,?) ON CONFLICT(user_id, business_id) DO UPDATE SET"
            " role = excluded.role",
            (user_id, business_id, str(role), int(time.time())))
        self.conn.commit()

    def remove_member(self, business_id: str, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM memberships WHERE business_id = ? AND user_id = ?",
            (business_id, user_id))
        self.conn.commit()

    def role_in(self, user: Optional[User], business_id: str) -> Optional[Role]:
        """
        What this user may do in this business.

        An operator is NOT automatically an owner. Running the platform is not
        the same as being entitled to edit a customer's contract, and conflating
        them is how an operator silently changes what a merchant is owed.
        """
        if user is None:
            return None
        row = self.conn.execute(
            "SELECT role FROM memberships WHERE user_id = ? AND business_id = ?",
            (user.user_id, business_id)).fetchone()
        return Role(row["role"]) if row else None

    def businesses_for(self, user: User, include_archived: bool = False) -> list:
        archived = "" if include_archived else " AND b.archived_at IS NULL"
        return self.conn.execute(
            "SELECT b.*, m.role, ("
            "  SELECT COUNT(*) FROM live_payments p WHERE p.business_id = b.business_id"
            " ) AS payments"
            " FROM businesses b JOIN memberships m ON m.business_id = b.business_id"
            f" WHERE m.user_id = ?{archived} ORDER BY b.created_at",
            (user.user_id,)).fetchall()

    def archived_for(self, user: User) -> list:
        return self.conn.execute(
            "SELECT b.*, m.role FROM businesses b"
            " JOIN memberships m ON m.business_id = b.business_id"
            " WHERE m.user_id = ? AND b.archived_at IS NOT NULL"
            " ORDER BY b.archived_at DESC", (user.user_id,)).fetchall()

    def members_of(self, business_id: str) -> list:
        return self.conn.execute(
            "SELECT u.user_id, u.email, u.name, m.role, m.added_at"
            " FROM memberships m JOIN users u ON u.user_id = m.user_id"
            " WHERE m.business_id = ? ORDER BY m.added_at", (business_id,)).fetchall()

    def owner_count(self, business_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM memberships WHERE business_id = ? AND role = ?",
            (business_id, str(Role.OWNER))).fetchone()["n"]
