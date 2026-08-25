"""
Where a business's settlement data comes from.

## The thing this file exists to make obvious

This platform is not where sales happen. It is where sales get CHECKED
afterwards. A merchant's money already flows through their gateway; we read
what the gateway did to it and say which parts were wrong.

That was not obvious from the app, because the first page anyone saw was a form
for typing in sales - which made it look like a very bad point-of-sale system.
The form is a stand-in for a data connection, and this module makes that
explicit by turning "where does the data come from" into a thing you choose.

    RAZORPAY    read the merchant's real settlement reports over the API.
                This is the product.

    SIMULATOR   manufacture settlements locally so the auditor has something
                to audit. This is scaffolding, and it is labelled as such
                everywhere it appears.

Everything downstream is identical either way. The detector, the agent, the
gate, the dispute letters and the audit log neither know nor care which side
the settlement came from - that is the point, and it is why the connector
lives here rather than being tangled into the pipeline.

## Why live keys are refused

Pulling a merchant's real settlements needs their API secret. This prototype has
no login: anyone who can reach the page can open any business on it. Storing a
live secret in a plaintext SQLite file under those conditions is indefensible,
so the connector refuses anything that is not a test key, and the secret is
never written to disk at all - it is asked for at sync time and used once.

The consequence is honest and worth stating out loud: test mode does not
settle, so a test connection will usually find zero settlements. The connector
is real; the data behind it is empty by design.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

API = "https://api.razorpay.com/v1"

SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_sources (
  business_id     TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,     -- 'simulator' | 'razorpay'
  razorpay_key_id TEXT,              -- the PUBLIC half. Safe in the clear.
  -- The secret, encrypted with a key held OUTSIDE this file. NULL when no
  -- encryption key is configured, in which case the secret is not stored at
  -- all and is asked for at sync time. There is deliberately no column that
  -- could hold it in the clear.
  razorpay_secret_encrypted TEXT,
  connected_at    INTEGER,
  last_sync_at    INTEGER,
  last_status     TEXT,
  last_message    TEXT
);
"""


class SourceKind(StrEnum):
    SIMULATOR = "simulator"
    RAZORPAY = "razorpay"


KIND_LABEL = {
    SourceKind.RAZORPAY: "Razorpay account",
    SourceKind.SIMULATOR: "Built-in simulator",
}

KIND_BLURB = {
    SourceKind.RAZORPAY:
        "Read this merchant's real settlement reports straight from Razorpay. "
        "This is what the product does in production.",
    SourceKind.SIMULATOR:
        "Manufacture settlements locally so the auditor has something to work "
        "on without a connected account. Demo data - no real gateway.",
}


@dataclass
class SyncResult:
    ok: bool
    message: str
    settlements_found: int = 0
    payments_found: int = 0
    raw: list = field(default_factory=list)


def is_test_key(key_id: str) -> bool:
    return (key_id or "").startswith("rzp_test_")


class Razorpay:
    """A read-only client. Nothing here creates, captures or refunds anything."""

    def __init__(self, key_id: str, key_secret: str):
        from merchant.vault import live_keys_allowed

        if not is_test_key(key_id) and not live_keys_allowed():
            raise ValueError(
                "This install accepts test-mode keys only (rzp_test_...). "
                "Live credentials need an encryption key configured "
                "(LEDGERLINE_SECRET_KEY) and an explicit opt-in "
                "(LEDGERLINE_ALLOW_LIVE_KEYS=1) - and even then TLS, login "
                "rate limiting and an access log are still missing.")
        token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}

    def get(self, path: str):
        request = urllib.request.Request(API + path, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception as exc:                            # noqa: BLE001
            return None, {"transport_error": str(exc)}

    def check(self) -> SyncResult:
        """Confirm the keys work before storing anything about them."""
        status, body = self.get("/payments?count=1")
        if status == 200:
            return SyncResult(True, "Connected.")
        if status is None:
            return SyncResult(False, f"Could not reach Razorpay: "
                                     f"{body.get('transport_error')}")
        description = body.get("error", {}).get("description", str(status))
        return SyncResult(False, f"Razorpay rejected the keys: {description}")

    def settlements(self, year: int, month: int) -> SyncResult:
        """
        Pull one month of the settlement recon report.

        This is the endpoint the whole product is built around: it is the only
        place a gateway states, line by line, what it deducted and why. Our
        entire schema mirrors its column names.
        """
        status, body = self.get(
            f"/settlements/recon/combined?year={year}&month={month:02d}")
        if status != 200:
            description = body.get("error", {}).get("description", str(status))
            return SyncResult(False, f"Could not read settlements: {description}")

        rows = body.get("items", [])
        settlements = {r.get("settlement_id") for r in rows if r.get("settlement_id")}
        payments = [r for r in rows if r.get("type") == "payment"]
        if not rows:
            return SyncResult(
                True,
                f"Connected, but {month:02d}/{year} has no settlement lines. "
                f"Razorpay test mode does not settle, so this is expected - "
                f"the connector works, there is simply nothing behind it.",
                0, 0, [])
        return SyncResult(True, f"Read {len(rows)} settlement lines.",
                          len(settlements), len(payments), rows)


class Sources:
    """Which source each business uses. Wraps an open connection."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(SOURCE_SCHEMA)
        self.conn.commit()

    def get(self, business_id: str):
        return self.conn.execute(
            "SELECT * FROM data_sources WHERE business_id = ?",
            (business_id,)).fetchone()

    def kind(self, business_id: str) -> Optional[SourceKind]:
        row = self.get(business_id)
        return SourceKind(row["kind"]) if row else None

    def use_simulator(self, business_id: str) -> None:
        # Switching away from Razorpay drops the stored secret. Keeping a
        # credential for a connection nobody is using is how one outlives the
        # reason it existed.
        self.forget_secret(business_id)
        self._set(business_id, SourceKind.SIMULATOR, None, "ok",
                  "Using locally generated demo data.")

    def connect_razorpay(self, business_id: str, key_id: str,
                         key_secret: str, remember: bool = True) -> SyncResult:
        """
        Verify the keys, then store the public id - and the secret only if
        there is somewhere safe to put it.

        With an encryption key configured the secret is stored encrypted, so a
        scheduled sync can run without anyone present. Without one it is used
        for this call and dropped, and every later sync asks again. What never
        happens is the middle option: storing it in the clear because storing
        it was convenient.
        """
        from merchant.vault import Vault

        try:
            client = Razorpay(key_id, key_secret)
        except ValueError as exc:
            return SyncResult(False, str(exc))

        result = client.check()
        if not result.ok:
            return result

        vault = Vault.from_env()
        encrypted = None
        if remember and vault is not None:
            encrypted = vault.encrypt(key_secret)
            result = SyncResult(
                True, result.message + " The secret is stored encrypted, so "
                                       "syncs can run unattended.")
        elif remember:
            result = SyncResult(
                True, result.message + " No encryption key is configured, so "
                                       "the secret was not stored - each sync "
                                       "will ask for it again.")

        self._set(business_id, SourceKind.RAZORPAY, key_id, "ok",
                  result.message, encrypted)
        return result

    def stored_secret(self, business_id: str) -> Optional[str]:
        """
        The decrypted secret, or None.

        None means "ask the person" - covering no vault, nothing stored, a key
        that has been rotated away, and a ciphertext someone has edited. All
        four have the same correct response, so they are not distinguished.
        """
        from merchant.vault import Vault

        row = self.get(business_id)
        if row is None or not row["razorpay_secret_encrypted"]:
            return None
        vault = Vault.from_env()
        if vault is None:
            return None
        return vault.decrypt(row["razorpay_secret_encrypted"])

    def forget_secret(self, business_id: str) -> None:
        self.conn.execute(
            "UPDATE data_sources SET razorpay_secret_encrypted = NULL"
            " WHERE business_id = ?", (business_id,))
        self.conn.commit()

    def record_sync(self, business_id: str, result: SyncResult) -> None:
        self.conn.execute(
            "UPDATE data_sources SET last_sync_at = ?, last_status = ?,"
            " last_message = ? WHERE business_id = ?",
            (int(time.time()), "ok" if result.ok else "error", result.message,
             business_id))
        self.conn.commit()

    def disconnect(self, business_id: str) -> None:
        self.conn.execute("DELETE FROM data_sources WHERE business_id = ?",
                          (business_id,))
        self.conn.commit()

    def _set(self, business_id: str, kind: SourceKind, key_id: Optional[str],
             status: str, message: str,
             encrypted_secret: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO data_sources (business_id, kind, razorpay_key_id,"
            " razorpay_secret_encrypted, connected_at, last_sync_at,"
            " last_status, last_message)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET"
            " kind = excluded.kind, razorpay_key_id = excluded.razorpay_key_id,"
            " razorpay_secret_encrypted ="
            "   COALESCE(excluded.razorpay_secret_encrypted,"
            "            data_sources.razorpay_secret_encrypted),"
            " connected_at = excluded.connected_at,"
            " last_status = excluded.last_status,"
            " last_message = excluded.last_message",
            (business_id, str(kind), key_id, encrypted_secret, int(time.time()),
             int(time.time()), status, message))
        self.conn.commit()
