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
  last_message    TEXT,
  -- Mode A of the supplier history service: a GSP or GST verification API the
  -- merchant holds their own key for. Only the URL template is stored in the
  -- clear; the key follows the same rule as the Razorpay secret, encrypted
  -- with a key held outside this file or not stored at all. There is
  -- deliberately no plaintext column for it.
  filing_api_url        TEXT,
  filing_api_key_header TEXT,
  filing_api_key_param  TEXT,
  filing_api_key_encrypted TEXT,
  filing_api_status     TEXT,
  filing_api_message    TEXT,
  filing_api_checked_at INTEGER
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
    SourceKind.RAZORPAY: "Real settlement reports, straight from Razorpay.",
    SourceKind.SIMULATOR: "Generated settlements - no gateway needed.",
}


@dataclass
class SyncResult:
    ok: bool
    message: str
    settlements_found: int = 0
    payments_found: int = 0
    invoices_found: int = 0
    disputes_found: int = 0
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
                "(ARTHAI_SECRET_KEY) and an explicit opt-in "
                "(ARTHAI_ALLOW_LIVE_KEYS=1) - and even then TLS, login "
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
                0, 0, 0, 0, [])
        return SyncResult(True, f"Read {len(rows)} settlement lines.",
                          len(settlements), len(payments), 0, 0, rows)

    def captured_payments(self, count: int = 100) -> SyncResult:
        """
        Pull captured payments, which carry Razorpay's own `fee` and `tax`.

        This exists because test mode never settles. The settlement recon
        report is the richer source and stays the primary one, but on a test
        account it is permanently empty - so an auditor that could only read
        it could never run on real Razorpay data at all.

        A captured payment states the amount, the instrument and what
        Razorpay charged for it, which is every input the expected-value
        engine needs. What it does NOT carry is a settlement date or a UTR,
        so agents that measure settlement timing cannot use this: it answers
        "was I charged the right rate", not "did the money arrive on time".
        """
        status, body = self.get(
            f"/payments?count={min(count, 100)}")
        if status != 200:
            description = body.get("error", {}).get("description", str(status))
            return SyncResult(False, f"Could not read payments: {description}")

        rows = [r for r in body.get("items", [])
                if r.get("captured") and r.get("status") == "captured"]
        if not rows:
            return SyncResult(
                True,
                "Connected, but no captured payments came back. A payment has "
                "to complete through Checkout before it carries a fee.",
                0, 0, 0, 0, [])
        return SyncResult(True, f"Read {len(rows)} captured payments.",
                          0, len(rows), 0, 0, rows)

    def invoices(self, count: int = 100) -> SyncResult:
        """
        Pull real outward invoices for the GST output-tax reconciler -
        alongside settlements(), never replacing it; a different Razorpay
        product (Invoices, not the settlement recon report) feeding a
        different agent (gst_filing's layer 1, not settlement_audit).

        GSTIN, HSN/SAC code and tax rate are real, documented fields on
        this endpoint, but Razorpay's own API can only CREATE an invoice
        without them - a person fills them in through the Dashboard. A
        merchant who never has is not a connector failure; it is read the
        same way engine.gst_filing.razorpay_import already reads it: never
        guessed, always named to the merchant. See that module's docstring.
        """
        status, body = self.get(f"/invoices?count={min(count, 100)}")
        if status != 200:
            description = body.get("error", {}).get("description", str(status))
            return SyncResult(False, f"Could not read invoices: {description}")

        rows = body.get("items", [])
        if not rows:
            return SyncResult(
                True,
                f"Connected, but no invoices came back. Razorpay test mode "
                f"has no real invoices unless some were created by hand "
                f"through the test-mode Dashboard - this is expected, the "
                f"connector works, there is simply nothing behind it.",
                0, 0, 0, 0, [])
        return SyncResult(True, f"Read {len(rows)} invoices.", 0, 0,
                          len(rows), 0, rows)

    def disputes(self, count: int = 100) -> SyncResult:
        """
        Pull real chargebacks for the chargeback defence assembler -
        alongside settlements()/invoices(), a third Razorpay product (the
        Disputes API), feeding a third agent. Unlike a rate card or a
        vendor's contracted price, the dispute NOTICE itself - reason code,
        amount, deadline - genuinely is real and fetchable here; it is only
        the EVIDENCE behind it (delivery proof, a customer's chat log) that
        no API anywhere supplies. See engine/chargeback/razorpay_import.py's
        docstring for the exact field names this reads.
        """
        status, body = self.get(f"/disputes?count={min(count, 100)}")
        if status != 200:
            description = body.get("error", {}).get("description", str(status))
            return SyncResult(False, f"Could not read disputes: {description}")

        rows = body.get("items", [])
        if not rows:
            return SyncResult(
                True,
                f"Connected, but no disputes came back. Razorpay test mode "
                f"does not generate real chargebacks - this is expected, "
                f"the connector works, there is simply nothing behind it.",
                0, 0, 0, 0, [])
        return SyncResult(True, f"Read {len(rows)} disputes.", 0, 0, 0,
                          len(rows), rows)


class Sources:
    """Which source each business uses. Wraps an open connection."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(SOURCE_SCHEMA)
        # CREATE TABLE IF NOT EXISTS does nothing to a table that already
        # exists, so the filing-API columns never reach a database that
        # predates them without this.
        from merchant.businesses import _add_column

        for column, ddl in (("filing_api_url", "TEXT"),
                            ("filing_api_key_header", "TEXT"),
                            ("filing_api_key_param", "TEXT"),
                            ("filing_api_key_encrypted", "TEXT"),
                            ("filing_api_status", "TEXT"),
                            ("filing_api_message", "TEXT"),
                            ("filing_api_checked_at", "INTEGER")):
            _add_column(conn, "data_sources", column, ddl)
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


    # --- mode A configuration --------------------------------------------------
    #
    # The filing-status API is configured per business and lives on the same row as
    # the settlement connector, because both answer "where does this merchant's
    # real data come from" and a merchant who has one usually has the other.
    #
    # It is a separate method rather than a flag on connect_razorpay because they
    # fail independently: a Razorpay connection saying nothing about GST filings is
    # the normal case, not an error.

    def configure_filing_api(self, business_id: str, *, url_template: str,
                             api_key: str = "", key_header: str = "",
                             key_param: str = "", remember: bool = True,
                             probe_gstin: str = "", http=None) -> SyncResult:
        """
        Store a filing-status API for this business, after checking it answers.

        The key follows exactly the rule the Razorpay secret follows: encrypted
        with a key held outside the database, or not stored at all. What never
        happens is the middle option - storing it in the clear because storing it
        was convenient.

        `probe_gstin` is optional. When given, the URL is actually called once
        before anything is saved, so a merchant finds out the configuration is
        wrong now rather than in the middle of a fifty-supplier run.
        """
        from merchant.gstin_lookup import FilingStatusApi
        from merchant.vault import Vault

        url_template = (url_template or "").strip()
        if "{gstin}" not in url_template:
            return SyncResult(
                False, "The URL needs a {gstin} placeholder - that is where each "
                       "supplier's number is substituted in.")
        if not url_template.lower().startswith("https://"):
            # A GST API key in a query string over plain HTTP is a credential
            # broadcast to every hop in between.
            return SyncResult(False, "The URL must be https.")
        if api_key and not (key_header or key_param):
            return SyncResult(
                False, "Say where the key goes - a header name or a query "
                       "parameter name.")

        message = "Saved."
        if probe_gstin:
            client = FilingStatusApi(url_template=url_template, api_key=api_key,
                                     key_header=key_header, key_param=key_param,
                                     http=http)
            history = client.history_for(probe_gstin)
            if client.failures:
                return SyncResult(
                    False, f"That did not work: {client.failures[0][1]}. Nothing "
                           f"was saved.")
            message = (f"Connected. Read {len(history.months)} tax periods for "
                       f"{probe_gstin}.")

        vault = Vault.from_env()
        encrypted = None
        if api_key and remember and vault is not None:
            encrypted = vault.encrypt(api_key)
            message += " The key is stored encrypted."
        elif api_key and remember:
            message += (" No encryption key is configured, so the API key was not "
                        "stored - set ARTHAI_SECRET_KEY to keep it.")

        self.conn.execute(
            "INSERT INTO data_sources (business_id, kind, connected_at,"
            " filing_api_url, filing_api_key_header, filing_api_key_param,"
            " filing_api_key_encrypted, filing_api_status, filing_api_message,"
            " filing_api_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(business_id) DO UPDATE SET"
            " filing_api_url = excluded.filing_api_url,"
            " filing_api_key_header = excluded.filing_api_key_header,"
            " filing_api_key_param = excluded.filing_api_key_param,"
            " filing_api_key_encrypted ="
            "   COALESCE(excluded.filing_api_key_encrypted,"
            "            data_sources.filing_api_key_encrypted),"
            " filing_api_status = excluded.filing_api_status,"
            " filing_api_message = excluded.filing_api_message,"
            " filing_api_checked_at = excluded.filing_api_checked_at",
            (business_id, str(SourceKind.SIMULATOR), int(time.time()),
             url_template, key_header.strip(), key_param.strip(), encrypted,
             "ok", message, int(time.time())))
        self.conn.commit()
        return SyncResult(True, message)


    def filing_api_config(self, business_id: str) -> Optional[dict]:
        """
        The stored configuration, with the key decrypted if it can be.

        A configuration whose key cannot be decrypted still comes back - the URL is
        real and the merchant needs to see it is there. `key_available` says
        whether a run can actually use it, which is the question the caller has.
        """
        from merchant.vault import Vault

        row = self.get(business_id)
        if row is None or not row["filing_api_url"]:
            return None

        key = ""
        if row["filing_api_key_encrypted"]:
            vault = Vault.from_env()
            key = (vault.decrypt(row["filing_api_key_encrypted"]) or "") if vault else ""

        needs_key = bool(row["filing_api_key_header"] or row["filing_api_key_param"])
        return {
            "url_template": row["filing_api_url"],
            "key_header": row["filing_api_key_header"] or "",
            "key_param": row["filing_api_key_param"] or "",
            "api_key": key,
            "key_available": bool(key) or not needs_key,
            "status": row["filing_api_status"] or "",
            "message": row["filing_api_message"] or "",
            "checked_at": row["filing_api_checked_at"] or 0,
        }


    def disconnect_filing_api(self, business_id: str) -> None:
        self.conn.execute(
            "UPDATE data_sources SET filing_api_url = NULL,"
            " filing_api_key_header = NULL, filing_api_key_param = NULL,"
            " filing_api_key_encrypted = NULL, filing_api_status = NULL,"
            " filing_api_message = NULL WHERE business_id = ?", (business_id,))
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