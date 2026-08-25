"""
Tests for the supplier watch.

Most of these are about SILENCE. A watch is easy to write and hard to make
bearable: the failure mode is not missing something, it is saying so much that
the merchant stops reading, at which point missing something is guaranteed.

So the decoy here - a supplier who is six weeks late every month, forever -
matters more than the supplier who stops. Catching the one that stops is the
demo. Never mentioning the one that is merely always late is the product.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst.history import (ALWAYS_LATE, DIES, NEWCOMER,  # noqa: E402
                                RELIABLE, STOPS, SupplierScript,
                                generate_history)
from engine.gst.watch import (DEADLINE_NEAR, EXPOSURE_ROSE,  # noqa: E402
                              FILING_SLIPPED, FIRST_SEEN, REGISTRATION_DIED,
                              RESUMED_FILING, SILENT_PERIODS, STATUS_ACTIVE,
                              STATUS_CANCELLED, STATUS_UNKNOWN, STOPPED_FILING,
                              CHANGE_LABEL, diff, ranked, snapshot,
                              total_exposure)


@pytest.fixture(scope="module")
def history():
    batches, statuses = generate_history(6)
    active = {g: {"status": STATUS_ACTIVE} for g in statuses}
    snaps = [snapshot(b, statuses=(statuses if i >= 4 else active))
             for i, b in enumerate(batches)]
    return batches, snaps


def _kinds(changes) -> set:
    return {c.kind for c in changes}


def _for(changes, name):
    return [c for c in changes if c.name == name]


# --- the snapshot counts, it does not judge ------------------------------

def test_a_supplier_appears_once_however_many_invoices_they_sent(history):
    _batches, snaps = history
    last = snaps[-1]
    names = [s.name for s in last.values()]
    assert len(names) == len(set(names)), "the same supplier counted twice"


def test_filing_rate_is_an_observed_frequency(history):
    """
    Not a probability. "Filed 3 of 6" is a fact; "50% likely to file next time"
    is a forecast, and CLAUDE.md section 3 rules those out on purpose.
    """
    _batches, snaps = history
    stopper = next(s for s in snaps[-1].values()
                   if s.name == "Deepak Packaging")
    assert stopper.filing_rate_bps == (stopper.invoices_filed * 10_000
                                       // stopper.invoices_booked)


def test_exposure_is_the_tax_on_what_they_did_not_file(history):
    _batches, snaps = history
    for state in snaps[-1].values():
        if state.invoices_missing == 0:
            assert state.exposed_paise == 0
        else:
            assert state.exposed_paise > 0


def test_suppliers_rank_by_how_much_of_your_money_they_hold(history):
    _batches, snaps = history
    order = [s.exposed_paise for s in ranked(snaps[-1])]
    assert order == sorted(order, reverse=True)


def test_a_supplier_nobody_looked_up_is_unknown_not_active():
    """
    "We did not check" and "we checked and it was fine" must never read alike.
    An unknown registration that defaults to active is a silent false negative.
    """
    batches, _statuses = generate_history(2)
    state = next(iter(snapshot(batches[-1]).values()))
    assert state.status == STATUS_UNKNOWN
    assert not state.is_dead


# --- silence, which is the hard part -------------------------------------

def test_nothing_is_said_when_nothing_changed(history):
    _batches, snaps = history
    assert diff(snaps[0], snaps[1]) == []
    assert diff(snaps[1], snaps[2]) == []


def test_a_supplier_who_is_always_late_is_never_mentioned(history):
    """
    The decoy. Kaveri files six weeks late every single month and always will.
    On any one month's reconciliation they look exactly like a problem; across
    time they are simply how Kaveri works.
    """
    _batches, snaps = history
    for i in range(1, len(snaps)):
        assert not _for(diff(snaps[i - 1], snaps[i]), "Kaveri Silk Mills"), \
            f"the watch complained about a supplier who never changed (month {i})"


def test_a_standing_problem_is_not_re_reported_every_month(history):
    """
    Deepak stops filing once. That is news once. Saying it again next month is
    how a watch gets muted, and a muted watch misses the next real thing.
    """
    _batches, snaps = history
    stopped_in = [i for i in range(1, len(snaps))
                  if STOPPED_FILING in _kinds(_for(diff(snaps[i - 1], snaps[i]),
                                                   "Deepak Packaging"))]
    assert len(stopped_in) == 1, f"reported as stopped {len(stopped_in)} times"


def test_a_reliable_supplier_never_produces_a_change(history):
    _batches, snaps = history
    for i in range(1, len(snaps)):
        for name in ("Anand Textiles", "Coimbatore Yarns"):
            assert not _for(diff(snaps[i - 1], snaps[i]), name)


# --- the events that ARE worth noticing ----------------------------------

def test_a_supplier_who_stops_filing_is_caught(history):
    _batches, snaps = history
    found = any(STOPPED_FILING in _kinds(_for(diff(snaps[i - 1], snaps[i]),
                                              "Deepak Packaging"))
                for i in range(1, len(snaps)))
    assert found


def test_the_first_miss_is_caught_before_it_becomes_a_stoppage(history):
    """One missed filing is a slip; two periods of silence is a stoppage. Both
    are worth knowing, and they are not the same event."""
    _batches, snaps = history
    slipped = [i for i in range(1, len(snaps))
               if FILING_SLIPPED in _kinds(_for(diff(snaps[i - 1], snaps[i]),
                                                "Deepak Packaging"))]
    stopped = [i for i in range(1, len(snaps))
               if STOPPED_FILING in _kinds(_for(diff(snaps[i - 1], snaps[i]),
                                                "Deepak Packaging"))]
    assert slipped and stopped
    assert min(slipped) < min(stopped)


def test_buying_more_from_a_silent_supplier_is_new_information(history):
    """
    The one thing that IS news about a standing problem: the merchant kept
    buying, so the exposure grew.
    """
    _batches, snaps = history
    rose = [c for i in range(1, len(snaps))
            for c in _for(diff(snaps[i - 1], snaps[i]), "Deepak Packaging")
            if c.kind == EXPOSURE_ROSE]
    assert rose
    assert "still not filed" in rose[0].detail


def test_a_cancelled_registration_is_caught(history):
    _batches, snaps = history
    died = [c for i in range(1, len(snaps))
            for c in _for(diff(snaps[i - 1], snaps[i]), "Vayu Motors")
            if c.kind == REGISTRATION_DIED]
    assert died
    assert died[0].was == STATUS_ACTIVE
    assert died[0].now == STATUS_CANCELLED


def test_a_new_supplier_is_noticed(history):
    _batches, snaps = history
    seen = [c for i in range(1, len(snaps))
            for c in _for(diff(snaps[i - 1], snaps[i]), "Gurgaon Warehousing")
            if c.kind == FIRST_SEEN]
    assert seen


def test_a_supplier_who_starts_filing_again_is_reported_as_good_news():
    cast = [SupplierScript("On Again Ltd", "27", STOPS, 100_000_00,
                           changes_in_month=2)]
    batches, statuses = generate_history(3, cast=cast)
    # hand-build the recovery: the third snapshot has them filing again
    before = snapshot(batches[1])
    after = snapshot(batches[1])
    key = next(iter(after))
    after[key].periods_since_filing = 0
    after[key].last_filed_period = "2026-05"
    before[key].periods_since_filing = SILENT_PERIODS + 1
    assert RESUMED_FILING in _kinds(diff(before, after))


# --- every change carries its numbers ------------------------------------

def test_every_change_is_quotable_without_further_arithmetic(history):
    _batches, snaps = history
    for i in range(1, len(snaps)):
        for change in diff(snaps[i - 1], snaps[i]):
            assert change.detail
            assert change.name
            assert change.kind in CHANGE_LABEL
            assert isinstance(change.exposed_paise, int)


def test_total_exposure_is_the_sum_of_the_parts(history):
    _batches, snaps = history
    assert total_exposure(snaps[-1]) == sum(
        s.exposed_paise for s in snaps[-1].values())


# --- how much history is enough ------------------------------------------

def test_a_rate_from_one_or_two_invoices_is_not_reported_as_a_rate():
    """
    A supplier with a single unfiled invoice read "0%" and sat in the same
    column as one showing "100%" from twelve. Identical presentation, wildly
    different evidence, and the reader has no way to tell them apart.
    """
    from engine.gst.watch import MIN_INVOICES_FOR_RATE, SupplierState

    thin = SupplierState(gstin="A", name="New Ltd", invoices_booked=1,
                         invoices_filed=0)
    assert not thin.enough_to_judge

    thick = SupplierState(gstin="B", name="Known Ltd",
                          invoices_booked=MIN_INVOICES_FOR_RATE,
                          invoices_filed=1)
    assert thick.enough_to_judge


def test_a_stoppage_cannot_be_seen_without_enough_periods():
    """
    The most valuable finding needs a supplier to have filed and then stopped,
    which takes SILENT_PERIODS of silence to observe. One month of GSTR-2B can
    reconcile invoices; it cannot judge a supplier, and the product should not
    imply otherwise.
    """
    from engine.gst.watch import (MIN_PERIODS_TO_JUDGE, SILENT_PERIODS,
                                  STOPPED_FILING, diff, snapshot)

    batches, _statuses = generate_history(1)
    assert MIN_PERIODS_TO_JUDGE > 1

    only = snapshot(batches[0])
    # Nothing to compare a single period against, so nothing is claimed.
    assert diff({}, only) and all(
        c.kind != STOPPED_FILING for c in diff({}, only))


def test_enough_history_does_surface_a_stoppage():
    """The other half: given the periods, the finding must actually appear."""
    from engine.gst.watch import (MIN_PERIODS_TO_JUDGE, STATUS_ACTIVE,
                                  STOPPED_FILING, diff, snapshot)

    batches, statuses = generate_history(6)
    active = {g: {"status": STATUS_ACTIVE} for g in statuses}
    snaps = [snapshot(b, statuses=active) for b in batches]
    assert len(snaps) >= MIN_PERIODS_TO_JUDGE

    kinds = {c.kind for i in range(1, len(snaps))
             for c in diff(snaps[i - 1], snaps[i])}
    assert STOPPED_FILING in kinds
