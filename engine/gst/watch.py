"""
Watching suppliers over time, rather than reconciling one month.

## The difference this module exists to make

A reconciliation answers "what is wrong today". It runs when somebody asks,
reports, and stops. That is a dashboard.

This answers "what CHANGED, and is it worth interrupting somebody for". The
second half of that question is the whole point: a supplier who is six weeks
late every single month is not news, and a supplier who filed for eight months
and has now stopped is - even though on any single month's reconciliation they
look identical.

## Where the line between calculator and agent falls here

Same rule as everywhere else in this system (CLAUDE.md section 2), and it is
worth being precise because it is easy to put in the wrong place:

    the CALCULATOR works out what moved.  Filed 8 of 8 last time, 8 of 11 now.
                                          Last filed two periods ago. Rs
                                          1,22,400 exposed. 94 days to the
                                          deadline. All arithmetic.

    the AGENT decides whether that is worth waking somebody up for, what to do
                                          about it, and how to say it.

That second decision is the one a script cannot make. A threshold either fires
on everything, which trains the merchant to ignore it, or it needs a number
somebody hand-tuned, which breaks on the first unusual supplier.

## Nothing here predicts anything

A supplier's filing rate is an observed frequency, not a probability of future
behaviour. "Failed to file 8 of their last 14" is a fact nobody can argue with;
"73% likely to file" is a number nobody can check. CLAUDE.md section 3 rules
out the second, and the first is more useful anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.gst import rules

# GSTIN registration states, as the public GST portal reports them. UNKNOWN is
# the honest default: we have not looked, which is not the same as active.
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

DEAD_STATUSES = frozenset({STATUS_SUSPENDED, STATUS_CANCELLED})


@dataclass
class SupplierState:
    """One supplier's picture at one moment. Every field is counted, not judged."""
    gstin: str
    name: str
    invoices_booked: int = 0
    invoices_filed: int = 0
    tax_booked: int = 0                     # paise
    tax_filed: int = 0
    exposed_paise: int = 0                  # tax on invoices they have not filed
    last_filed_period: Optional[str] = None
    periods_since_filing: int = 0
    earliest_exposed_deadline: Optional[date] = None
    days_to_earliest_deadline: Optional[int] = None
    status: str = STATUS_UNKNOWN
    status_changed_on: Optional[str] = None

    @property
    def invoices_missing(self) -> int:
        return self.invoices_booked - self.invoices_filed

    @property
    def filing_rate_bps(self) -> int:
        """Observed frequency in basis points. Not a forecast."""
        if not self.invoices_booked:
            return 0
        return (self.invoices_filed * 10_000) // self.invoices_booked

    @property
    def enough_to_judge(self) -> bool:
        """
        Whether a rate on this supplier means anything yet.

        Reported rather than hidden: "we have not seen enough of this supplier"
        is a useful thing for a merchant to know, and it is very different from
        "this supplier is unreliable" - which is what a bare 0% looked like.
        """
        return self.invoices_booked >= MIN_INVOICES_FOR_RATE

    @property
    def files_reliably(self) -> bool:
        """Filed everything booked. Deliberately strict - 'mostly' is not a
        state you want a merchant relying on for their tax credit."""
        return self.invoices_booked > 0 and self.invoices_missing == 0

    @property
    def is_dead(self) -> bool:
        return self.status in DEAD_STATUSES


@dataclass
class Change:
    """
    Something that moved between two snapshots, with the numbers worked out.

    `detail` is written to be quotable verbatim. The agent reads it and decides
    what it means; it never has to compute anything to understand it.
    """
    kind: str
    gstin: str
    name: str
    detail: str
    exposed_paise: int = 0
    was: str = ""
    now: str = ""
    days_to_deadline: Optional[int] = None


# What the calculator can observe. Whether any of it is worth raising is not
# decided here.
STOPPED_FILING = "stopped_filing"
RESUMED_FILING = "resumed_filing"
FILING_SLIPPED = "filing_slipped"
EXPOSURE_ROSE = "exposure_rose"
DEADLINE_NEAR = "deadline_near"
REGISTRATION_DIED = "registration_died"
FIRST_SEEN = "first_seen"

CHANGE_LABEL = {
    STOPPED_FILING: "Stopped filing",
    RESUMED_FILING: "Started filing again",
    FILING_SLIPPED: "Filing less reliably than before",
    EXPOSURE_ROSE: "More of your credit now depends on them",
    DEADLINE_NEAR: "Exposed credit is running out of time",
    REGISTRATION_DIED: "GST registration is no longer active",
    FIRST_SEEN: "A supplier you have not bought from before",
}

# How long a supplier can go without filing before it counts as having stopped
# rather than merely being late. Two periods, because one is ordinary.
SILENT_PERIODS = 2

# Below this many invoices, a filing rate is one or two data points wearing a
# percentage sign.
#
# A supplier with a single unfiled invoice reported "0%" and sat in the same
# column as one showing "100%" from twelve - identical presentation, wildly
# different evidence. Three is not statistically magic; it is the point below
# which the number invites a conclusion the data cannot support.
MIN_INVOICES_FOR_RATE = 3

# And the watch cannot see a STOPPAGE at all without enough periods to observe
# one: a supplier has to have filed, then not filed, for SILENT_PERIODS. One
# month of GSTR-2B can reconcile invoices; it cannot judge a supplier.
MIN_PERIODS_TO_JUDGE = SILENT_PERIODS + 1

# A deadline is "near" inside this window. s.16(4) gives between eight and
# twenty months depending on when in the year the invoice fell, so a fixed
# number of days is the only comparable measure.
DEADLINE_WINDOW_DAYS = 120


def snapshot(batch, *, statuses: Optional[dict] = None,
             today: Optional[date] = None) -> dict[str, SupplierState]:
    """
    Every supplier in the batch, counted.

    `statuses` optionally maps GSTIN to a registration status looked up from
    the public GST portal. Absent, every supplier is UNKNOWN - which is
    deliberately not the same as active, because "we did not check" and "we
    checked and it was fine" must never read alike.
    """
    today = today or batch.as_of
    statuses = statuses or {}

    filed_keys: dict[str, list] = {}
    for line in batch.gstr2b:
        filed_keys.setdefault(line.supplier_gstin.strip().upper(), []).append(line)

    out: dict[str, SupplierState] = {}

    for invoice in batch.purchases:
        key = invoice.supplier_gstin.strip().upper()
        state = out.get(key)
        if state is None:
            info = statuses.get(key) or {}
            state = SupplierState(
                gstin=key, name=invoice.supplier_name,
                status=info.get("status", STATUS_UNKNOWN),
                status_changed_on=info.get("changed_on"))
            out[key] = state

        theirs = filed_keys.get(key, [])
        matched = next(
            (l for l in theirs
             if l.invoice_number.strip().upper()
             == invoice.invoice_number.strip().upper()), None)

        state.invoices_booked += 1
        state.tax_booked += invoice.total_tax

        if matched is not None:
            state.invoices_filed += 1
            state.tax_filed += matched.total_tax
            period = matched.filed_period
            if state.last_filed_period is None or period > state.last_filed_period:
                state.last_filed_period = period
        else:
            state.exposed_paise += invoice.total_tax
            deadline = rules.claim_deadline(invoice.invoice_date)
            if (state.earliest_exposed_deadline is None
                    or deadline < state.earliest_exposed_deadline):
                state.earliest_exposed_deadline = deadline

    for state in out.values():
        state.periods_since_filing = _periods_between(
            state.last_filed_period, batch.period)
        if state.earliest_exposed_deadline is not None:
            state.days_to_earliest_deadline = (
                state.earliest_exposed_deadline - today).days

    return out


def _periods_between(earlier: Optional[str], later: str) -> int:
    """Whole months between two YYYY-MM periods. Never negative."""
    if not earlier or not later:
        return 0
    try:
        y1, m1 = (int(p) for p in earlier.split("-"))
        y2, m2 = (int(p) for p in later.split("-"))
    except (ValueError, AttributeError):
        return 0
    return max(0, (y2 - y1) * 12 + (m2 - m1))


def diff(previous: dict[str, SupplierState],
         current: dict[str, SupplierState]) -> list[Change]:
    """
    What moved. Facts only - nothing here decides what is worth raising.

    A supplier who was silent last time and is silent now produces NO change,
    which is the important half: a watch that re-reports a standing problem
    every morning is a watch that gets muted.
    """
    changes: list[Change] = []

    for gstin, now in current.items():
        before = previous.get(gstin)

        if before is None:
            changes.append(Change(
                kind=FIRST_SEEN, gstin=gstin, name=now.name,
                detail=(f"{now.name} is new to your register: "
                        f"{now.invoices_booked} invoice"
                        f"{'' if now.invoices_booked == 1 else 's'} carrying "
                        f"{rules.rupees(now.tax_booked)} of credit, of which "
                        f"{rules.rupees(now.exposed_paise)} is not yet filed."),
                exposed_paise=now.exposed_paise,
                now=f"{now.invoices_filed}/{now.invoices_booked} filed"))
            continue

        if now.is_dead and not before.is_dead:
            changes.append(Change(
                kind=REGISTRATION_DIED, gstin=gstin, name=now.name,
                detail=(f"{now.name}'s GST registration is now {now.status}"
                        f"{f' as of {now.status_changed_on}' if now.status_changed_on else ''}. "
                        f"{rules.rupees(now.tax_booked)} has been claimed "
                        f"against this registration."),
                exposed_paise=now.tax_booked,
                was=before.status, now=now.status))

        stopped_now = now.periods_since_filing >= SILENT_PERIODS
        stopped_before = before.periods_since_filing >= SILENT_PERIODS

        if stopped_now and not stopped_before:
            changes.append(Change(
                kind=STOPPED_FILING, gstin=gstin, name=now.name,
                detail=(f"{now.name} filed reliably up to "
                        f"{now.last_filed_period or 'never'} and has filed "
                        f"nothing for {now.periods_since_filing} periods since. "
                        f"{rules.rupees(now.exposed_paise)} of your credit "
                        f"depends on them."),
                exposed_paise=now.exposed_paise,
                was=f"last filed {before.last_filed_period}",
                now=f"silent {now.periods_since_filing} periods",
                days_to_deadline=now.days_to_earliest_deadline))
        elif stopped_before and not stopped_now:
            changes.append(Change(
                kind=RESUMED_FILING, gstin=gstin, name=now.name,
                detail=(f"{now.name} has started filing again, in "
                        f"{now.last_filed_period}. Credit that was at risk is "
                        f"now supported."),
                exposed_paise=now.exposed_paise,
                was=f"silent {before.periods_since_filing} periods",
                now=f"filed {now.last_filed_period}"))
        elif stopped_before and stopped_now:
            # Still silent. That is not news - it was reported when it started,
            # and a watch that repeats a standing problem every morning is a
            # watch that gets muted. The ONE thing here that is new is if the
            # merchant kept buying from them, which grows the exposure.
            if now.exposed_paise > before.exposed_paise:
                changes.append(Change(
                    kind=EXPOSURE_ROSE, gstin=gstin, name=now.name,
                    detail=(f"You bought another "
                            f"{rules.rupees(now.exposed_paise - before.exposed_paise)} "
                            f"from {now.name}, who has still not filed "
                            f"anything since {now.last_filed_period}. Exposure "
                            f"is now {rules.rupees(now.exposed_paise)}."),
                    exposed_paise=now.exposed_paise,
                    was=rules.rupees(before.exposed_paise),
                    now=rules.rupees(now.exposed_paise),
                    days_to_deadline=now.days_to_earliest_deadline))
        elif now.filing_rate_bps < before.filing_rate_bps and now.invoices_missing:
            changes.append(Change(
                kind=FILING_SLIPPED, gstin=gstin, name=now.name,
                detail=(f"{now.name} filed {before.invoices_filed} of "
                        f"{before.invoices_booked} invoices before and "
                        f"{now.invoices_filed} of {now.invoices_booked} now. "
                        f"{rules.rupees(now.exposed_paise)} is unfiled."),
                exposed_paise=now.exposed_paise,
                was=f"{before.filing_rate_bps / 100:.0f}%",
                now=f"{now.filing_rate_bps / 100:.0f}%",
                days_to_deadline=now.days_to_earliest_deadline))

        if (now.exposed_paise > before.exposed_paise
                and not stopped_now and now.filing_rate_bps >= before.filing_rate_bps):
            changes.append(Change(
                kind=EXPOSURE_ROSE, gstin=gstin, name=now.name,
                detail=(f"Unfiled credit with {now.name} rose from "
                        f"{rules.rupees(before.exposed_paise)} to "
                        f"{rules.rupees(now.exposed_paise)}."),
                exposed_paise=now.exposed_paise,
                was=rules.rupees(before.exposed_paise),
                now=rules.rupees(now.exposed_paise),
                days_to_deadline=now.days_to_earliest_deadline))

        near_now = (now.days_to_earliest_deadline is not None
                    and 0 <= now.days_to_earliest_deadline <= DEADLINE_WINDOW_DAYS)
        near_before = (before.days_to_earliest_deadline is not None
                       and 0 <= before.days_to_earliest_deadline <= DEADLINE_WINDOW_DAYS)
        if near_now and not near_before and now.exposed_paise:
            changes.append(Change(
                kind=DEADLINE_NEAR, gstin=gstin, name=now.name,
                detail=(f"{rules.rupees(now.exposed_paise)} of unfiled credit "
                        f"with {now.name} must be claimed by "
                        f"{now.earliest_exposed_deadline} - "
                        f"{now.days_to_earliest_deadline} days away. After "
                        f"that it is gone."),
                exposed_paise=now.exposed_paise,
                now=f"{now.days_to_earliest_deadline} days",
                days_to_deadline=now.days_to_earliest_deadline))

    return changes


def total_exposure(states: dict[str, SupplierState]) -> int:
    return sum(s.exposed_paise for s in states.values())


def ranked(states: dict[str, SupplierState]) -> list[SupplierState]:
    """Suppliers by how much of the merchant's money they are sitting on."""
    return sorted(states.values(),
                  key=lambda s: (-s.exposed_paise, s.filing_rate_bps, s.name))
