"""
Looking up whether a supplier's GST registration is still alive.

## Why there is no scraper here, and there never will be

The GST portal's Search Taxpayer page is public and free, and it is protected
by a captcha. Every unofficial "API" for it works by relaying that captcha to a
human and replaying the session. This module does not do that, for two reasons
and the first one is sufficient:

    1. Defeating bot protection is not something this project does. It is
       somebody else's infrastructure and their access terms are theirs to set.
    2. It would break the week the portal changed its markup, which is exactly
       the wrong dependency to put under a finding that accuses a real company
       of having a dead registration.

So a lookup comes from one of two places: a verification API the merchant has
their own key for, or a person who looked it up on the portal themselves and
typed in what they saw. Both are honest. Neither pretends to be the other.

## Why the answer is cached globally rather than per business

A GSTIN's registration status is a public fact about a company, not a fact
about whoever asked. Two merchants who both buy from Anand Textiles should not
each spend a lookup, and the answer cannot differ between them. The cache is
therefore shared - and it holds nothing about who looked, because that is the
part that would belong to a tenant.

## Staleness is stated, never hidden

A cancellation that happened yesterday is invisible to a lookup done last
month. So every result carries when it was checked, and anything past the
freshness window is reported as stale rather than quietly served as current. A
status with no date on it is worse than no status at all: it invites a merchant
to act on something nobody has verified recently.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Protocol

from engine.gst.watch import (STATUS_ACTIVE, STATUS_CANCELLED,
                              STATUS_SUSPENDED, STATUS_UNKNOWN)

# How long a lookup stays current. A month is a compromise: registrations do
# not change often, and a merchant who checks quarterly should still be told
# their information is old rather than shown it as fact.
FRESH_FOR_DAYS = 30

SOURCE_MANUAL = "manual"
SOURCE_API = "api"

# What providers call the states, mapped to what this system calls them.
STATUS_WORDS = {
    "active": STATUS_ACTIVE,
    "act": STATUS_ACTIVE,
    "cancelled": STATUS_CANCELLED,
    "canceled": STATUS_CANCELLED,
    "cnl": STATUS_CANCELLED,
    "inactive": STATUS_CANCELLED,
    "suspended": STATUS_SUSPENDED,
    "sus": STATUS_SUSPENDED,
    "provisional": STATUS_ACTIVE,
}


def normalise_status(word: Optional[str]) -> str:
    """
    A provider's word for a status, in ours.

    Anything unrecognised becomes UNKNOWN rather than being guessed at. A
    provider that starts returning a state we have never seen must not have it
    silently rounded to "active", which is the reading that causes harm.
    """
    if not word:
        return STATUS_UNKNOWN
    return STATUS_WORDS.get(str(word).strip().lower(), STATUS_UNKNOWN)


@dataclass
class LookupResult:
    gstin: str
    status: str = STATUS_UNKNOWN
    legal_name: str = ""
    trade_name: str = ""
    registered_on: Optional[str] = None
    cancelled_on: Optional[str] = None
    last_return_filed: Optional[str] = None
    source: str = SOURCE_MANUAL
    checked_at: int = field(default_factory=lambda: int(time.time()))
    note: str = ""

    @property
    def known(self) -> bool:
        return self.status != STATUS_UNKNOWN

    def stale_after(self, now: Optional[int] = None) -> bool:
        now = now or int(time.time())
        return (now - self.checked_at) > FRESH_FOR_DAYS * 86_400

    def as_status(self) -> dict:
        """The shape engine.gst.watch.snapshot expects."""
        return {"status": self.status, "changed_on": self.cancelled_on}


class Provider(Protocol):
    name: str

    def lookup(self, gstin: str) -> LookupResult: ...


class HttpProvider:
    """
    A verification API the merchant has their own key for.

    Deliberately generic. Half a dozen vendors sell this and they differ only
    in the URL and where they put the key, so hard-coding one would be picking
    a favourite on the merchant's behalf and stranding anyone who already pays
    somebody else.
    """

    name = "api"

    def __init__(self, *, url_template: str, api_key: str = "",
                 key_header: str = "", key_param: str = "", http=None):
        self.url_template = url_template
        self.api_key = api_key
        self.key_header = key_header
        self.key_param = key_param
        self._http = http

    def lookup(self, gstin: str) -> LookupResult:
        url = self.url_template.replace("{gstin}", gstin.strip().upper())
        headers = {self.key_header: self.api_key} if self.key_header else {}
        params = {self.key_param: self.api_key} if self.key_param else {}

        try:
            response = self._request("GET", url, headers=headers, params=params)
        except Exception as exc:                            # noqa: BLE001
            return LookupResult(gstin=gstin, source=SOURCE_API,
                                note=f"lookup failed: {exc}")

        if response.status_code != 200:
            return LookupResult(
                gstin=gstin, source=SOURCE_API,
                note=f"provider returned {response.status_code}")

        try:
            payload = response.json()
        except Exception:                                   # noqa: BLE001
            return LookupResult(gstin=gstin, source=SOURCE_API,
                                note="provider returned something that is not JSON")
        return from_payload(gstin, payload)

    def _request(self, method, url, **kw):
        if self._http is not None:
            return self._http(method, url, **kw)
        import httpx

        with httpx.Client(timeout=15) as client:
            return client.request(method, url, **kw)


# Field names the common providers use, in the order we try them. Listed rather
# than discovered so that a provider adding a field cannot silently change what
# this system believes about a registration.
STATUS_FIELDS = ("sts", "status", "gstin_status", "registration_status",
                 "taxpayer_status", "current_registration_status")
LEGAL_FIELDS = ("lgnm", "legal_name", "legalName", "legal_name_of_business")
TRADE_FIELDS = ("tradeNam", "trade_name", "tradeName", "business_name")
REGISTERED_FIELDS = ("rgdt", "registration_date", "date_of_registration")
CANCELLED_FIELDS = ("cxdt", "cancellation_date", "date_of_cancellation",
                    "cancelled_date")


def _first(payload: dict, names) -> Optional[str]:
    for name in names:
        value = payload.get(name)
        if value not in (None, "", "NA"):
            return str(value)
    return None


def from_payload(gstin: str, payload: dict) -> LookupResult:
    """
    A provider's JSON, in our shape.

    Most wrap the government's own field names one or two levels down, so the
    obvious containers are unwrapped first.
    """
    for key in ("data", "result", "taxpayerInfo", "gstin_details"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            payload = {**payload, **inner}

    status = normalise_status(_first(payload, STATUS_FIELDS))
    return LookupResult(
        gstin=gstin.strip().upper(),
        status=status,
        legal_name=_first(payload, LEGAL_FIELDS) or "",
        trade_name=_first(payload, TRADE_FIELDS) or "",
        registered_on=_first(payload, REGISTERED_FIELDS),
        cancelled_on=_first(payload, CANCELLED_FIELDS),
        source=SOURCE_API,
        note="" if status != STATUS_UNKNOWN
             else "the provider did not report a status we recognise")


# --- the cache -------------------------------------------------------------

LOOKUP_SCHEMA = """
-- Public facts about companies, not about whoever asked. No business_id here
-- on purpose: two merchants buying from the same supplier get the same answer
-- and should not each pay for it, and nothing in this table belongs to either
-- of them.
CREATE TABLE IF NOT EXISTS gstin_status (
  gstin             TEXT PRIMARY KEY,
  status            TEXT,
  legal_name        TEXT,
  trade_name        TEXT,
  registered_on     TEXT,
  cancelled_on      TEXT,
  last_return_filed TEXT,
  source            TEXT,
  note              TEXT,
  checked_at        INTEGER
);
"""


class GstinStatus:
    """The shared cache of registration lookups."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(LOOKUP_SCHEMA)
        self.conn.commit()

    def get(self, gstin: str) -> Optional[LookupResult]:
        row = self.conn.execute(
            "SELECT * FROM gstin_status WHERE gstin = ?",
            (gstin.strip().upper(),)).fetchone()
        if row is None:
            return None
        return LookupResult(
            gstin=row["gstin"], status=row["status"],
            legal_name=row["legal_name"] or "",
            trade_name=row["trade_name"] or "",
            registered_on=row["registered_on"],
            cancelled_on=row["cancelled_on"],
            last_return_filed=row["last_return_filed"],
            source=row["source"] or SOURCE_MANUAL,
            checked_at=row["checked_at"] or 0, note=row["note"] or "")

    def put(self, result: LookupResult) -> None:
        self.conn.execute(
            "INSERT INTO gstin_status (gstin, status, legal_name, trade_name,"
            " registered_on, cancelled_on, last_return_filed, source, note,"
            " checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(gstin) DO UPDATE SET status = excluded.status,"
            " legal_name = excluded.legal_name,"
            " trade_name = excluded.trade_name,"
            " registered_on = excluded.registered_on,"
            " cancelled_on = excluded.cancelled_on,"
            " last_return_filed = excluded.last_return_filed,"
            " source = excluded.source, note = excluded.note,"
            " checked_at = excluded.checked_at",
            (result.gstin.strip().upper(), result.status, result.legal_name,
             result.trade_name, result.registered_on, result.cancelled_on,
             result.last_return_filed, result.source, result.note,
             result.checked_at))
        self.conn.commit()

    def statuses_for(self, gstins) -> dict:
        """
        What the watch needs: GSTIN to status, for the ones we actually know.

        A stale entry is deliberately EXCLUDED rather than served. The watch
        treats a missing status as "not checked", which is the truthful reading
        of a lookup nobody has refreshed in a month - and it means a stale
        "active" can never mask a cancellation that happened since.
        """
        out = {}
        for gstin in gstins:
            found = self.get(gstin)
            if found is not None and found.known and not found.stale_after():
                out[found.gstin] = found.as_status()
        return out

    def record_manual(self, gstin: str, status: str,
                      cancelled_on: Optional[str] = None,
                      legal_name: str = "") -> LookupResult:
        """What a person saw on the portal and typed in. Marked as such."""
        result = LookupResult(
            gstin=gstin.strip().upper(), status=normalise_status(status),
            legal_name=legal_name, cancelled_on=cancelled_on,
            source=SOURCE_MANUAL,
            note="entered by hand from the GST portal")
        self.put(result)
        return result

    def refresh(self, gstins, provider: Provider,
                on_each: Optional[Callable[[LookupResult], None]] = None
                ) -> list[LookupResult]:
        """Look up each GSTIN we do not have a fresh answer for."""
        out = []
        for gstin in gstins:
            existing = self.get(gstin)
            if existing is not None and existing.known \
                    and not existing.stale_after():
                continue
            result = provider.lookup(gstin)
            self.put(result)
            out.append(result)
            if on_each is not None:
                on_each(result)
        return out
