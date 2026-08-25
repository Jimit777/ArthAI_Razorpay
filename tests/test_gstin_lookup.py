"""
Tests for the GSTIN registration lookup.

Two things are being protected. The first is that a STALE answer never gets
served as a current one - a month-old "active" is not evidence a registration
is alive today, and serving it as though it were is how a merchant keeps
claiming credit against a company that was cancelled in between.

The second is that this module never becomes a scraper. The portal's public
search is captcha-protected, and there is a test asserting nothing here tries
to get around that.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst.watch import (STATUS_ACTIVE, STATUS_CANCELLED,  # noqa: E402
                              STATUS_SUSPENDED, STATUS_UNKNOWN)
from merchant.gstin_lookup import (FRESH_FOR_DAYS, GstinStatus,  # noqa: E402
                                   HttpProvider, LookupResult, SOURCE_API,
                                   SOURCE_MANUAL, from_payload,
                                   normalise_status)

GSTIN = "27AABCU9603R1ZM"


@pytest.fixture
def cache(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "g.db")
    conn.row_factory = sqlite3.Row
    return GstinStatus(conn)


class Reply:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


# --- reading a provider's answer -----------------------------------------

def test_the_common_status_words_are_understood():
    assert normalise_status("Active") == STATUS_ACTIVE
    assert normalise_status("Cancelled") == STATUS_CANCELLED
    assert normalise_status("SUS") == STATUS_SUSPENDED


def test_a_word_nobody_has_seen_before_becomes_unknown():
    """
    Never rounded to active. That is the reading that causes harm - it tells a
    merchant a registration is fine when nobody established that.
    """
    assert normalise_status("liquidated") == STATUS_UNKNOWN
    assert normalise_status(None) == STATUS_UNKNOWN
    assert normalise_status("") == STATUS_UNKNOWN


def test_the_governments_own_field_names_are_read():
    result = from_payload(GSTIN, {"sts": "Cancelled", "lgnm": "Vayu Motors",
                                  "cxdt": "14/05/2026"})
    assert result.status == STATUS_CANCELLED
    assert result.legal_name == "Vayu Motors"
    assert result.cancelled_on == "14/05/2026"


def test_a_payload_wrapped_one_level_down_is_unwrapped():
    result = from_payload(GSTIN, {"data": {"status": "Active",
                                           "legal_name": "Anand Textiles"}})
    assert result.status == STATUS_ACTIVE
    assert result.legal_name == "Anand Textiles"


def test_a_provider_that_reports_nothing_useful_says_so():
    result = from_payload(GSTIN, {"whatever": "hello"})
    assert result.status == STATUS_UNKNOWN
    assert result.note


# --- the provider ---------------------------------------------------------

def test_a_provider_error_is_a_failed_lookup_not_an_active_registration():
    def http(method, url, **kw):
        raise ConnectionError("no route to host")

    result = HttpProvider(url_template="https://x/{gstin}", http=http).lookup(GSTIN)
    assert result.status == STATUS_UNKNOWN
    assert "lookup failed" in result.note


def test_a_non_200_is_reported():
    provider = HttpProvider(url_template="https://x/{gstin}",
                            http=lambda *a, **k: Reply({}, 503))
    assert "503" in provider.lookup(GSTIN).note


def test_the_gstin_goes_into_the_url():
    seen = {}

    def http(method, url, **kw):
        seen["url"] = url
        return Reply({"sts": "Active"})

    HttpProvider(url_template="https://x/verify/{gstin}", http=http).lookup(GSTIN)
    assert GSTIN in seen["url"]


def test_the_key_can_go_in_a_header_or_a_parameter():
    seen = {}

    def http(method, url, **kw):
        seen.update(kw)
        return Reply({"sts": "Active"})

    HttpProvider(url_template="https://x/{gstin}", api_key="k",
                 key_header="X-Api-Key", http=http).lookup(GSTIN)
    assert seen["headers"]["X-Api-Key"] == "k"

    HttpProvider(url_template="https://x/{gstin}", api_key="k",
                 key_param="token", http=http).lookup(GSTIN)
    assert seen["params"]["token"] == "k"


# --- staleness ------------------------------------------------------------

def test_a_fresh_answer_is_served(cache):
    cache.put(LookupResult(gstin=GSTIN, status=STATUS_ACTIVE))
    assert cache.statuses_for([GSTIN])[GSTIN]["status"] == STATUS_ACTIVE


def test_a_stale_answer_is_withheld_rather_than_served(cache):
    """
    The important one. A month-old "active" excluded means the watch treats the
    supplier as unchecked - which is truthful, and means a stale active can
    never mask a cancellation that happened since.
    """
    old = int(time.time()) - (FRESH_FOR_DAYS + 5) * 86_400
    cache.put(LookupResult(gstin=GSTIN, status=STATUS_ACTIVE, checked_at=old))
    assert cache.statuses_for([GSTIN]) == {}


def test_an_unknown_answer_is_never_served(cache):
    cache.put(LookupResult(gstin=GSTIN, status=STATUS_UNKNOWN))
    assert cache.statuses_for([GSTIN]) == {}


def test_a_cancellation_date_travels_with_the_status(cache):
    cache.put(LookupResult(gstin=GSTIN, status=STATUS_CANCELLED,
                           cancelled_on="14/05/2026"))
    assert cache.statuses_for([GSTIN])[GSTIN]["changed_on"] == "14/05/2026"


def test_refresh_skips_what_is_already_fresh(cache):
    calls = []

    class Counting:
        name = "test"

        def lookup(self, gstin):
            calls.append(gstin)
            return LookupResult(gstin=gstin, status=STATUS_ACTIVE)

    cache.put(LookupResult(gstin=GSTIN, status=STATUS_ACTIVE))
    cache.refresh([GSTIN, "29OTHER1234F1Z5"], Counting())
    assert calls == ["29OTHER1234F1Z5"]


# --- what a person typed in ----------------------------------------------

def test_a_hand_entered_status_is_marked_as_hand_entered(cache):
    result = cache.record_manual(GSTIN, "cancelled", "14/05/2026")
    assert result.source == SOURCE_MANUAL
    assert "by hand" in result.note
    assert cache.get(GSTIN).status == STATUS_CANCELLED


def test_a_hand_entered_status_is_used_by_the_watch(cache):
    cache.record_manual(GSTIN, "suspended")
    assert cache.statuses_for([GSTIN])[GSTIN]["status"] == STATUS_SUSPENDED


# --- the cache is shared, and holds nothing about who asked --------------

def test_the_cache_holds_no_tenant_data(cache):
    """
    A registration status is a public fact about a company, not about whoever
    looked it up. A business_id in here would make it one.
    """
    columns = {c[1] for c in cache.conn.execute(
        "PRAGMA table_info(gstin_status)")}
    assert "business_id" not in columns
    assert "user_id" not in columns


# --- and it never becomes a scraper --------------------------------------

def test_nothing_here_tries_to_get_past_the_portals_captcha():
    """
    The portal's public search is captcha-protected. Every unofficial wrapper
    for it relays that captcha to a human and replays the session. This project
    does not, and this test is here so that stays true by accident as well as
    by intention.
    """
    source = (Path(__file__).parent.parent / "merchant"
              / "gstin_lookup.py").read_text().lower()
    for banned in ("captcha", "services.gst.gov.in/services/api",
                   "beautifulsoup", "selenium", "playwright"):
        if banned == "captcha":
            # The word appears in the docstring explaining the refusal; what
            # must not appear is code that solves or submits one.
            assert "captcha_solve" not in source
            assert "solve_captcha" not in source
            continue
        assert banned not in source
