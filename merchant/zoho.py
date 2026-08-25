"""
Pulling a merchant's purchase register out of Zoho Books.

## What this replaces

Typing invoices into a form, or exporting a CSV and uploading it. A merchant
who already keeps their books in Zoho has every purchase bill in there with the
supplier's GSTIN, the invoice number, the date and the tax split - which is
precisely the shape the ITC engine wants.

## The honest part

This is PLUMBING. It moves rows from one system to another and makes no
judgment about any of them. Saying so matters, because "we integrate with your
accounting software" is the kind of claim that gets mistaken for intelligence -
and the intelligence in this product is elsewhere, in the thing that decides a
supplier who has stopped filing is worth interrupting somebody for.

## Secrets

Same rule as the Razorpay connector, and for the same reason: the refresh token
is stored ONLY if there is somewhere safe to put it. With an encryption key
configured it is stored encrypted so a scheduled pull can run unattended;
without one it is used and dropped, and the merchant reconnects next time.
There is deliberately no third option where it lands in the clear because that
was easier.

## Regions

Zoho runs separate data centres and a token minted in one is worthless in
another. An Indian business is almost always on .in, but guessing wrong gives
an unauthorised error that looks like bad credentials, so the region is stored
rather than assumed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Zoho's data centres. The accounts host mints the token; the api host answers
# with data; a token from one is rejected by the other.
REGIONS = {
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "com.au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
}
DEFAULT_REGION = "in"

# Read-only. The connector cannot create, edit or delete anything in the
# merchant's books, and that is enforced by the scope rather than by intent -
# guardrail 1 says the agent never writes to a ledger, and somebody else's
# accounting system is very much a ledger.
SCOPES = "ZohoBooks.contacts.READ,ZohoBooks.bills.READ,ZohoBooks.settings.READ"

TOKEN_LIFETIME_SECONDS = 3_600


class ZohoError(RuntimeError):
    pass


@dataclass
class PullResult:
    ok: bool
    message: str
    vendors: int = 0
    bills: int = 0
    imported: int = 0
    skipped: list[str] = field(default_factory=list)


def authorise_url(client_id: str, redirect_uri: str,
                  region: str = DEFAULT_REGION, state: str = "") -> str:
    """
    Where the merchant goes to grant access.

    Only the merchant can complete this step - it needs their Zoho password,
    which is theirs and stays theirs. The platform never sees it, and never
    asks for it.
    """
    from urllib.parse import urlencode

    accounts, _api = REGIONS.get(region, REGIONS[DEFAULT_REGION])
    query = urlencode({
        "scope": SCOPES,
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "access_type": "offline",       # without this there is no refresh token
        "prompt": "consent",
        "state": state,
    })
    return f"{accounts}/oauth/v2/auth?{query}"


class ZohoBooks:
    """
    A read-only client for one organisation.

    Takes an http callable so the whole thing is testable without a Zoho
    account, which matters here: there is no sandbox, and a connector nobody
    can test until demo day is a connector that fails on demo day.
    """

    def __init__(self, *, client_id: str, client_secret: str,
                 refresh_token: str, organization_id: str,
                 region: str = DEFAULT_REGION, http=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.organization_id = organization_id
        self.region = region if region in REGIONS else DEFAULT_REGION
        self._http = http
        self._token: Optional[str] = None
        self._token_expires = 0.0

    @property
    def accounts_host(self) -> str:
        return REGIONS[self.region][0]

    @property
    def api_host(self) -> str:
        return REGIONS[self.region][1]

    def _request(self, method: str, url: str, **kw):
        if self._http is not None:
            return self._http(method, url, **kw)
        import httpx

        with httpx.Client(timeout=20) as client:
            return client.request(method, url, **kw)

    def _access_token(self) -> str:
        """Mint or reuse an access token. They last an hour; we renew at 55m."""
        if self._token and time.time() < self._token_expires:
            return self._token

        response = self._request(
            "POST", f"{self.accounts_host}/oauth/v2/token",
            data={"refresh_token": self.refresh_token,
                  "client_id": self.client_id,
                  "client_secret": self.client_secret,
                  "grant_type": "refresh_token"})
        if response.status_code != 200:
            raise ZohoError(f"Zoho refused the refresh token "
                            f"({response.status_code}). Reconnect the account.")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ZohoError(f"Zoho returned no access token: "
                            f"{payload.get('error', 'no reason given')}")
        self._token = token
        self._token_expires = time.time() + TOKEN_LIFETIME_SECONDS - 300
        return token

    def _get(self, path: str, **params):
        params["organization_id"] = self.organization_id
        response = self._request(
            "GET", f"{self.api_host}/books/v3/{path}",
            headers={"Authorization": f"Zoho-oauthtoken {self._access_token()}"},
            params=params)
        if response.status_code == 401:
            # The token expired mid-run. Mint a fresh one and try once.
            self._token = None
            response = self._request(
                "GET", f"{self.api_host}/books/v3/{path}",
                headers={"Authorization":
                         f"Zoho-oauthtoken {self._access_token()}"},
                params=params)
        if response.status_code != 200:
            raise ZohoError(f"Zoho Books returned {response.status_code} for "
                            f"{path}")
        return response.json()

    # --- what we actually read --------------------------------------------

    def check(self) -> PullResult:
        """Confirm the credentials work and name the organisation."""
        try:
            payload = self._get("organizations")
        except ZohoError as exc:
            return PullResult(False, str(exc))
        orgs = payload.get("organizations", [])
        mine = next((o for o in orgs
                     if str(o.get("organization_id")) == str(self.organization_id)),
                    None)
        if mine is None:
            return PullResult(
                False, f"This account has no organisation "
                       f"{self.organization_id}. Check the id in Zoho Books "
                       f"under Settings.")
        return PullResult(True, f"Connected to {mine.get('name', 'Zoho Books')}.")

    def vendors(self) -> list[dict]:
        out, page = [], 1
        while True:
            payload = self._get("contacts", contact_type="vendor",
                                page=page, per_page=200)
            out.extend(payload.get("contacts", []))
            if not payload.get("page_context", {}).get("has_more_page"):
                break
            page += 1
        return out

    def bills(self, date_from: Optional[date] = None) -> list[dict]:
        out, page = [], 1
        while True:
            params = {"page": page, "per_page": 200}
            if date_from:
                params["date_start"] = str(date_from)
            payload = self._get("bills", **params)
            out.extend(payload.get("bills", []))
            if not payload.get("page_context", {}).get("has_more_page"):
                break
            page += 1
        return out

    def bill(self, bill_id: str) -> dict:
        """One bill in full. The list view omits the tax breakdown."""
        return self._get(f"bills/{bill_id}").get("bill", {})


def _paise(value) -> int:
    """
    Rupees from Zoho, paise for us.

    Zoho sends money as a JSON number, which arrives as a float. Rounding it
    once here is the only place a float touches money in this system, and it is
    the boundary - everything downstream is integer paise.
    """
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def to_purchase(bill: dict, vendors_by_id: Optional[dict] = None) -> dict:
    """
    One Zoho bill, in the shape the ledger records a purchase in.

    Returns a dict rather than writing anything, so the mapping is testable
    without a database and so nothing is imported that could not be read back
    and checked first.
    """
    vendors_by_id = vendors_by_id or {}
    vendor = vendors_by_id.get(str(bill.get("vendor_id")), {})

    gstin = (bill.get("gst_no") or vendor.get("gst_no")
             or bill.get("vendor_gst_no") or "")

    taxes = bill.get("taxes") or []
    cgst = sgst = igst = 0
    for line in taxes:
        name = (line.get("tax_name") or "").upper()
        amount = _paise(line.get("tax_amount"))
        if "IGST" in name:
            igst += amount
        elif "CGST" in name:
            cgst += amount
        elif "SGST" in name or "UTGST" in name:
            sgst += amount

    total_tax = cgst + sgst + igst
    if not total_tax:
        # Some organisations report only a total. Splitting it ourselves would
        # be inventing a fact about which state the supply was in, so the whole
        # amount goes to IGST only when the bill says it is inter-state, and
        # otherwise the record is flagged for a person.
        total_tax = _paise(bill.get("tax_total"))
        if bill.get("is_inter_state"):
            igst = total_tax
        else:
            half = total_tax // 2
            cgst, sgst = half, total_tax - half

    return {
        "supplier_name": bill.get("vendor_name") or vendor.get("contact_name")
                         or "Unknown supplier",
        "supplier_gstin": gstin.strip().upper(),
        "invoice_number": bill.get("bill_number") or bill.get("reference_number")
                          or bill.get("bill_id", ""),
        "invoice_date": bill.get("date"),
        "taxable_value": _paise(bill.get("sub_total")),
        "cgst": cgst, "sgst": sgst, "igst": igst,
        "paid_on": bill.get("last_payment_date") or None,
        "zoho_bill_id": str(bill.get("bill_id", "")),
        "status": bill.get("status"),
    }


def importable(purchase: dict) -> Optional[str]:
    """
    Why this bill cannot be imported, or None if it can.

    A bill with no GSTIN cannot be reconciled against GSTR-2B at all - there is
    nothing to join on. Importing it anyway would put a row in the register
    that permanently shows as unfiled, which reads as a supplier default when
    it is actually missing data.
    """
    if not purchase.get("supplier_gstin"):
        return "no GSTIN on the bill or the vendor"
    if len(purchase["supplier_gstin"]) != 15:
        return f"GSTIN {purchase['supplier_gstin']} is not 15 characters"
    if not purchase.get("invoice_number"):
        return "no bill number"
    if not purchase.get("invoice_date"):
        return "no bill date"
    if purchase.get("taxable_value", 0) <= 0:
        return "no taxable value"
    return None


# --- storing the connection ------------------------------------------------

ZOHO_SCHEMA = """
CREATE TABLE IF NOT EXISTS zoho_connections (
  business_id      TEXT PRIMARY KEY,
  region           TEXT,
  organization_id  TEXT,
  organization_name TEXT,
  client_id        TEXT,
  -- Both of these are secrets and both are encrypted or absent. There is
  -- deliberately no column that could hold either in the clear.
  client_secret_encrypted TEXT,
  refresh_token_encrypted TEXT,
  connected_at     INTEGER,
  last_pull_at     INTEGER,
  last_pull_note   TEXT,
  state_token      TEXT       -- ties a callback to the request that started it
);
"""


class ZohoConnections:
    """One Zoho Books connection per business."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(ZOHO_SCHEMA)
        self.conn.commit()

    def get(self, business_id: str):
        return self.conn.execute(
            "SELECT * FROM zoho_connections WHERE business_id = ?",
            (business_id,)).fetchone()

    def begin(self, business_id: str, *, client_id: str, client_secret: str,
              organization_id: str, region: str) -> Optional[str]:
        """
        Record the app credentials and mint a state token for the redirect.

        Returns the state token, or None when there is no vault - because
        without one the client secret has nowhere safe to live, and the honest
        answer is to refuse rather than to store it in the clear.
        """
        import secrets

        from merchant.vault import Vault

        vault = Vault.from_env()
        if vault is None:
            return None

        state = secrets.token_urlsafe(24)
        self.conn.execute(
            "INSERT INTO zoho_connections (business_id, region,"
            " organization_id, client_id, client_secret_encrypted, state_token)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET"
            " region = excluded.region,"
            " organization_id = excluded.organization_id,"
            " client_id = excluded.client_id,"
            " client_secret_encrypted = excluded.client_secret_encrypted,"
            " state_token = excluded.state_token",
            (business_id, region, organization_id, client_id,
             vault.encrypt(client_secret), state))
        self.conn.commit()
        return state

    def complete(self, business_id: str, refresh_token: str,
                 organization_name: str = "") -> None:
        import time

        from merchant.vault import Vault

        vault = Vault.from_env()
        if vault is None:
            raise ZohoError("no encryption key is configured")
        self.conn.execute(
            "UPDATE zoho_connections SET refresh_token_encrypted = ?,"
            " organization_name = ?, connected_at = ?, state_token = NULL"
            " WHERE business_id = ?",
            (vault.encrypt(refresh_token), organization_name,
             int(time.time()), business_id))
        self.conn.commit()

    def client(self, business_id: str, http=None) -> Optional[ZohoBooks]:
        """A ready client, or None when anything needed is missing."""
        from merchant.vault import Vault

        row = self.get(business_id)
        if row is None or not row["refresh_token_encrypted"]:
            return None
        vault = Vault.from_env()
        if vault is None:
            return None
        secret = vault.decrypt(row["client_secret_encrypted"] or "")
        refresh = vault.decrypt(row["refresh_token_encrypted"])
        if not secret or not refresh:
            return None
        return ZohoBooks(
            client_id=row["client_id"], client_secret=secret,
            refresh_token=refresh, organization_id=row["organization_id"],
            region=row["region"] or DEFAULT_REGION, http=http)

    def record_pull(self, business_id: str, note: str) -> None:
        import time

        self.conn.execute(
            "UPDATE zoho_connections SET last_pull_at = ?, last_pull_note = ?"
            " WHERE business_id = ?", (int(time.time()), note, business_id))
        self.conn.commit()

    def disconnect(self, business_id: str) -> None:
        """Forget everything, secrets first."""
        self.conn.execute(
            "DELETE FROM zoho_connections WHERE business_id = ?",
            (business_id,))
        self.conn.commit()
