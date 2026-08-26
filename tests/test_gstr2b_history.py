"""
Reading a stack of GSTR-2B files as supplier filing history.

## What this is guarding

Using GSTR-2B as filing history was refused twice before it was built, and the
objection was sound: those files state what suppliers REPORTED, and reading
their silence about payment as non-payment would report every supplier in a
merchant's book as having defaulted - the most serious finding this product
makes, on no evidence at all.

The answer was not to refuse a third time. It was a third state. Silence is
recorded as IGNORANCE, and only two things move a period out of it:

    the portal flagged Rule 37A / ITC unavailable for supplier default
        -> they did not pay. Known, and the government said so.

    a filing-history CSV with explicit GSTR-3B dates
        -> payment fully visible, the strongest evidence there is.

Everything else stays unknown, and the arithmetic in risk.py divides every
payment ratio by the periods where payment is actually visible. So the
dangerous reading is not merely discouraged, it is unreachable.

The first test below is the one that matters: twelve clean months of GSTR-2B
must NOT produce a defaulter.
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst.risk import (ACT_SAFE, ACT_WATCH,  # noqa: E402
                             PATTERN_DEFAULTER, PATTERN_PAYMENT_UNKNOWN,
                             profile, recommended_action)
from merchant.gstr2b_history import parse_files  # noqa: E402

CLEAN = "24FJAMH3956X5ZJ"
DEFAULTER = "27GQRIR1135W5ZQ"
VANISHED = "29NYOZN7564Z9ZV"

PERIODS = ["072025", "082025", "092025", "102025", "112025", "122025",
           "012026", "022026", "032026", "042026", "052026", "062026"]


def _file(period, suppliers):
    """A GSTR-2B file in the portal's own shape."""
    return json.dumps({"data": {
        "rtnprd": period, "gstin": "27AAAAA0000A1Z5",
        "docdata": {"b2b": [
            {"ctin": gstin, "trdnm": name, "supprd": period,
             "supfildt": filed,
             "inv": [{"inum": f"{name[:3].upper()}/{period}",
                      "idt": f"05/{period[:2]}/{period[2:]}",
                      "val": 118000, "itcavl": itc, "rsn": reason,
                      "itms": [{"itm_det": {"txval": 100000, "igst": 18000,
                                            "rt": 18}}]}]}
            for gstin, name, filed, itc, reason in suppliers]}}}).encode()


def _twelve_clean_months():
    return [(_file(p, [(CLEAN, "Anand Textiles", f"11/{p[:2]}/{p[2:]}",
                        "Y", "")]), f"2b_{p}.json") for p in PERIODS]


# --- the finding that must not happen ------------------------------------

def test_clean_gstr2b_does_not_produce_a_defaulter():
    """
    THE test. Twelve months of perfectly ordinary GSTR-2B, and the supplier
    must not be accused of not paying tax - because nothing in those files
    says anything at all about whether they paid.
    """
    result = parse_files(_twelve_clean_months())
    prof = profile(result.histories[CLEAN])

    assert prof.pattern != PATTERN_DEFAULTER
    assert prof.pattern == PATTERN_PAYMENT_UNKNOWN
    assert prof.sold_but_did_not_pay == 0
    assert prof.gstr3b_known_periods == 0
    assert prof.payment_history_known is False


def test_invisible_payment_is_never_safe_to_pay():
    """
    The other half of the same honesty.

    "No evidence they defaulted" is not "evidence they did not", and the whole
    reason s.16(2)(c) is dangerous is that a supplier can look perfect in every
    document a buyer holds while the tax was never paid.
    """
    result = parse_files(_twelve_clean_months())
    prof = profile(result.histories[CLEAN])

    assert recommended_action(prof) == ACT_WATCH
    assert recommended_action(prof) != ACT_SAFE


def test_the_score_is_not_inflated_by_marks_it_cannot_see():
    """Payment is most of the trust score. It must not be awarded by default -
    that would rate an unchecked supplier above a checked, blameless one."""
    result = parse_files(_twelve_clean_months())
    prof = profile(result.histories[CLEAN])
    assert 40 <= prof.trust_score <= 70, prof.trust_score


# --- what these files genuinely prove ------------------------------------

def test_a_rule_37a_flag_is_read_as_a_real_default():
    """
    When the portal DOES say a supplier did not pay, that is the strongest
    evidence available and it is used.
    """
    files = []
    for period in PERIODS:
        late = period.endswith("2026")
        files.append((_file(period, [
            (DEFAULTER, "Kaveri Silk", f"18/{period[:2]}/{period[2:]}",
             "N" if late else "Y",
             "Rule 37A - supplier has not filed GSTR-3B" if late else "")]),
            f"2b_{period}.json"))

    result = parse_files(files)
    prof = profile(result.histories[DEFAULTER])

    assert result.defaults_found == 6
    assert prof.sold_but_did_not_pay == 6
    assert prof.gstr3b_known_periods == 6
    assert prof.pattern == PATTERN_DEFAULTER


def test_a_credit_blocked_for_the_buyers_own_reason_is_not_a_default():
    """
    "This credit is not available to you" and "your supplier never paid" are
    different statements, and only one is grounds for holding their money. A
    place-of-supply block is the buyer's problem, not the supplier's conduct.
    """
    files = [(_file(p, [(CLEAN, "Anand Textiles", f"11/{p[:2]}/{p[2:]}", "N",
                         "POS and supplier state are same but recipient "
                         "state is different")]), f"2b_{p}.json")
             for p in PERIODS]

    prof = profile(parse_files(files).histories[CLEAN])
    assert prof.sold_but_did_not_pay == 0
    assert prof.pattern != PATTERN_DEFAULTER


def test_a_supplier_who_goes_quiet_shows_fewer_periods():
    """The signal no single GSTR-2B can carry, and a stack of them can."""
    files = []
    for period in PERIODS:
        rows = [(CLEAN, "Anand Textiles", f"11/{period[:2]}/{period[2:]}",
                 "Y", "")]
        if period in PERIODS[:5]:
            rows.append((VANISHED, "Deepak Packaging",
                         f"11/{period[:2]}/{period[2:]}", "Y", ""))
        files.append((_file(period, rows), f"2b_{period}.json"))

    result = parse_files(files)
    assert len(result.histories[CLEAN].months) == 12
    assert len(result.histories[VANISHED].months) == 5


def test_the_suppliers_own_filing_period_is_honoured():
    """
    An invoice from May appearing in a July GSTR-2B was filed LATE. Booking it
    as a July filing would hide exactly the thing worth seeing.
    """
    payload = json.dumps({"data": {
        "rtnprd": "072026", "gstin": "27AAAAA0000A1Z5",
        "docdata": {"b2b": [
            {"ctin": CLEAN, "trdnm": "Anand", "supprd": "052026",
             "supfildt": "28/07/2026",
             "inv": [{"inum": "A/1", "idt": "05/05/2026", "val": 118000,
                      "itms": [{"itm_det": {"txval": 100000, "igst": 18000}}]}]}
        ]}}}).encode()

    history = parse_files([(payload, "2b.json")]).histories[CLEAN]
    assert [m.period for m in history.months] == ["2026-05"]
    assert history.months[0].gstr1_late_days > 0


def test_a_period_nobody_bought_in_produces_no_row():
    """
    GSTR-2B is a statement about the merchant's OWN purchases, so a supplier
    is absent both when they filed nothing and when nothing was bought from
    them. Those are indistinguishable, so neither is counted.
    """
    files = [(_file(p, [(CLEAN, "Anand", f"11/{p[:2]}/{p[2:]}", "Y", "")]),
              f"2b_{p}.json") for p in PERIODS[:3]]
    history = parse_files(files).histories[CLEAN]

    assert len(history.months) == 3
    assert profile(history).periods == 3


# --- the contract is the same as every other source ----------------------

def test_it_lands_in_the_standard_contract():
    """The agent must not be able to tell where a history came from."""
    from engine.gst.filing_history import FilingHistory

    history = parse_files(_twelve_clean_months()).histories[CLEAN]
    assert isinstance(history, FilingHistory)
    assert history.gstin == CLEAN
    assert history.source == "file"

    rows = history.as_rows()
    assert set(rows[0]) == {
        "period", "gstr1_due", "gstr1_filed", "gstr1_late_days",
        "gstr3b_due", "gstr3b_filed", "gstr3b_late_days", "gstr3b_known",
        "sold_but_did_not_pay"}


def test_an_unreadable_file_is_reported_not_swallowed():
    result = parse_files([(b"{}", "empty.json"),
                          (b"not json at all", "broken.json")])
    assert not result.ok
    assert result.skipped


# --- the round trip through storage --------------------------------------
#
# Every test above reads the parser's output directly, and every one of them
# passed while the product was branding all six demo suppliers as defaulters.
# The parser was right; the DATABASE was lossy. `gstr3b_known` had no column,
# so a GSTR-2B history went in as "payment unknown" and came back out as
# "payment known, and they did not pay" - the exact accusation the tri-state
# exists to make unreachable.
#
# So these tests go through storage. A contract that only holds in memory is
# not a contract.

def test_payment_visibility_survives_being_stored(tmp_path):
    """The regression. In memory it was right; through SQLite it was not."""
    from merchant.ledger import Ledger

    class Imported:
        def __init__(self, histories):
            self.histories = histories
            self.filename = "2b.json"

    parsed = parse_files(_twelve_clean_months())

    led = Ledger(str(tmp_path / "t.db"))
    led.business_id = led.businesses.create("Meera's Boutique")
    led.replace_filing_history(Imported(parsed.histories))

    reloaded = led.filing_history()[CLEAN]
    assert all(not m.gstr3b_known for m in reloaded.months)

    prof = profile(reloaded)
    assert prof.payment_history_known is False
    assert prof.sold_but_did_not_pay == 0
    assert prof.pattern == PATTERN_PAYMENT_UNKNOWN
    assert prof.pattern != PATTERN_DEFAULTER


def test_a_real_default_also_survives_being_stored(tmp_path):
    """The other direction: a Rule 37A flag must not be lost either."""
    from merchant.ledger import Ledger

    class Imported:
        def __init__(self, histories):
            self.histories = histories
            self.filename = "2b.json"

    files = []
    for period in PERIODS:
        late = period.endswith("2026")
        files.append((_file(period, [
            (DEFAULTER, "Kaveri Silk", f"18/{period[:2]}/{period[2:]}",
             "N" if late else "Y",
             "Rule 37A - supplier has not filed GSTR-3B" if late else "")]),
            f"2b_{period}.json"))

    led = Ledger(str(tmp_path / "t.db"))
    led.business_id = led.businesses.create("Meera's Boutique")
    led.replace_filing_history(Imported(parse_files(files).histories))

    prof = profile(led.filing_history()[DEFAULTER])
    assert prof.sold_but_did_not_pay == 6
    assert prof.pattern == PATTERN_DEFAULTER


def test_a_csv_history_still_reads_as_fully_visible(tmp_path):
    """
    The column defaults to 1, so histories from a CSV - which DO carry payment
    dates - keep their meaning, including rows written before it existed.
    """
    from merchant.ledger import Ledger
    from merchant.purchase_import import (parse_filing_history,
                                          sample_filing_history)

    led = Ledger(str(tmp_path / "t.db"))
    led.business_id = led.businesses.create("Meera's Boutique")
    led.replace_filing_history(
        parse_filing_history(sample_filing_history().encode(), "h.csv"))

    for history in led.filing_history().values():
        assert all(m.gstr3b_known for m in history.months)
        assert profile(history).payment_history_known
