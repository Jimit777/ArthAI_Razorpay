"""
Turning a filing history into a profile. Pure arithmetic, no model.

## Which half of this is the agent's

The specification for this feature asked the LLM to compute the late-filing
percentage and return a risk probability as a float. Every one of those is
arithmetic, and CLAUDE.md section 2 is unambiguous: ask a model for a number
and you get one that is usually right and occasionally, silently, wrong. The
product IS accuracy, so the numbers are computed here.

What the agent does with them is in agent/risk_agent.py: read the pattern,
name it, and decide whether the merchant should hold payment. That is judgment,
and it is the part a script genuinely cannot do.

## Why the default rate is observed, not predicted

"73% likely to default" is a forecast nobody can check. "Reported sales in 25
of their last 36 months without paying the tax" is a fact, and multiplying this
month's exposure by that observed frequency gives a defensible number rather
than a confident one. CLAUDE.md section 3 rules out the first; the second is
what a careful accountant would do by hand.

## What coverage cannot tell you, and who can

A supplier who filed nothing in a period might have had nothing to file. From
filing history alone the two are indistinguishable, so coverage is a weak
signal and carries a correspondingly small weight - it is not tuned until the
demo looks right.

The reconciliation CAN tell them apart, because it knows which months the
merchant actually bought from that supplier. Silence in a month you bought is
SUPPLIER_NOT_FILED and lands in the findings; silence in a month you did not is
nothing at all. The two halves of this agent answer different questions on
purpose, and the risk score should not pretend to the certainty the
reconciliation has.

## Why GSTR-3B dominates the score

Under CGST s.16(2)(c), upheld in Bhandari Scrap Traders, credit exists only
where the supplier actually PAID. A supplier who files GSTR-1 punctually and
skips GSTR-3B produces a purchase register that reconciles perfectly against a
credit that does not exist - so a score driven by GSTR-1 punctuality would rate
the most dangerous supplier in the book as excellent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.gst.filing_history import FilingHistory
from engine.gst.rules import (SUPPLIER_PAYMENT_DAYS, claim_deadline,
                              payment_due_by)

# How recent behaviour is weighted. A supplier who defaulted three years ago
# and has filed cleanly since is not the same risk as one who started
# defaulting in March, and a flat average over 36 months hides the difference.
RECENT_MONTHS = 12

# Weights for the trust score, stated rather than tuned until the demo looked
# good. GSTR-3B compliance is most of it because s.16(2)(c) turns on payment,
# not on reporting.
W_COMPLIANCE = 45           # did they pay the tax on what they reported
W_COVERAGE = 20             # and did they report at all
W_RECENT = 20               # have they paid lately
W_PUNCTUALITY = 15          # and did it arrive on time

# Coverage exists because compliance alone cannot see total silence. Its
# denominator is GSTR-1 filings, so a supplier who files NOTHING - no sales
# reported, no tax paid - is absent from both sides of the ratio and scores as
# perfectly compliant. An erratic supplier silent for eight of thirty-six
# months rated 99/100 before this was added.
#
# The two risks are different and a merchant needs both: silence means the
# invoice never reaches your GSTR-2B at all, and non-payment means it reaches
# you and the credit still is not there.

# A delay of this many days or more scores zero on punctuality. Six months is
# roughly where a late filing starts threatening the s.16(4) deadline.
DELAY_FLOOR_DAYS = 180

# A registration that is not active caps the score here regardless of history.
# Whatever they did before, credit claimed against a dead registration comes
# back with interest.
DEAD_REGISTRATION_CAP = 15

PATTERN_CLEAN = "CLEAN_HISTORY"
PATTERN_LATE = "HABITUAL_LATE_FILER"
PATTERN_DEFAULTER = "GSTR3B_DEFAULTER"
PATTERN_ERRATIC = "ERRATIC"
PATTERN_THIN = "TOO_LITTLE_HISTORY"

PATTERN_LABEL = {
    PATTERN_CLEAN: "Clean history",
    PATTERN_LATE: "Habitual late filer",
    PATTERN_DEFAULTER: "Does not pay the tax",
    PATTERN_ERRATIC: "Erratic",
    PATTERN_THIN: "Too little history",
}

# Below this many periods, a rate is a couple of data points wearing a
# percentage sign. Same reasoning as the supplier watch.
MIN_PERIODS = 6


@dataclass
class RiskProfile:
    """Everything computable about a supplier, before anyone judges it."""
    gstin: str
    periods: int = 0
    gstr1_filed: int = 0
    gstr3b_filed: int = 0
    sold_but_did_not_pay: int = 0
    recent_sold_but_did_not_pay: int = 0
    recent_periods: int = 0
    avg_gstr3b_delay_days: int = 0
    worst_gstr3b_delay_days: int = 0
    registration_status: str = "active"
    suspensions: list[str] = field(default_factory=list)

    @property
    def enough_history(self) -> bool:
        return self.gstr1_filed >= MIN_PERIODS

    @property
    def compliance_bps(self) -> int:
        """
        Of the months they reported sales, how many did they pay the tax for.

        Denominator is GSTR-1 filings, not all periods: a month with no sales
        has no tax to pay, and counting it as a default would punish a supplier
        for being quiet.
        """
        if not self.gstr1_filed:
            return 0
        return (self.gstr3b_filed * 10_000) // self.gstr1_filed

    @property
    def coverage_bps(self) -> int:
        """Of all the periods, how many they filed a GSTR-1 for at all."""
        if not self.periods:
            return 0
        return (self.gstr1_filed * 10_000) // self.periods

    @property
    def silent_periods(self) -> int:
        return self.periods - self.gstr1_filed

    @property
    def default_rate_bps(self) -> int:
        """Observed frequency of reporting a sale and not paying the tax."""
        if not self.gstr1_filed:
            return 0
        return (self.sold_but_did_not_pay * 10_000) // self.gstr1_filed

    @property
    def recent_default_rate_bps(self) -> int:
        if not self.recent_periods:
            return self.default_rate_bps
        return (self.recent_sold_but_did_not_pay * 10_000) // self.recent_periods

    @property
    def registration_alive(self) -> bool:
        return self.registration_status == "active"

    @property
    def trust_score(self) -> int:
        """
        One to a hundred. Higher is safer.

        Deliberately not a probability. It is a weighted summary of things that
        already happened, and the weights are above where they can be argued
        with rather than buried in a tuning pass.
        """
        if not self.enough_history:
            # Not "risky" - unknown. Parked mid-scale so it sorts away from
            # both the trusted and the dangerous, and the UI says why.
            return 50

        compliance = (self.compliance_bps * W_COMPLIANCE) // 10_000
        coverage = (self.coverage_bps * W_COVERAGE) // 10_000
        recent = ((10_000 - self.recent_default_rate_bps) * W_RECENT) // 10_000

        delay = min(self.avg_gstr3b_delay_days, DELAY_FLOOR_DAYS)
        punctuality = (W_PUNCTUALITY * (DELAY_FLOOR_DAYS - delay)) // DELAY_FLOOR_DAYS

        score = max(1, min(100,
                            compliance + coverage + recent + punctuality))
        if not self.registration_alive:
            score = min(score, DEAD_REGISTRATION_CAP)
        return score

    @property
    def pattern(self) -> str:
        """
        The shape of the history, decided by arithmetic.

        The agent is asked to confirm or override this with its reasoning; it
        is computed first so that a failed model call still leaves the merchant
        with a usable answer rather than a blank column.
        """
        if not self.enough_history:
            return PATTERN_THIN
        if self.default_rate_bps >= 2_500:
            return PATTERN_DEFAULTER
        # Silence is its own pattern, and it has to be checked before the
        # clean case - a supplier who files perfectly in the months they file
        # at all still leaves gaps a merchant has to know about.
        if self.coverage_bps < 9_000:
            return PATTERN_ERRATIC
        if self.compliance_bps >= 9_500 and self.avg_gstr3b_delay_days >= 60:
            return PATTERN_LATE
        if self.compliance_bps >= 9_500 and self.avg_gstr3b_delay_days <= 3:
            return PATTERN_CLEAN
        return PATTERN_ERRATIC

    def as_dict(self) -> dict:
        return {
            "gstin": self.gstin,
            "periods": self.periods,
            "gstr1_filed": self.gstr1_filed,
            "gstr3b_filed": self.gstr3b_filed,
            "sold_but_did_not_pay": self.sold_but_did_not_pay,
            "compliance_pct": round(self.compliance_bps / 100, 1),
            "coverage_pct": round(self.coverage_bps / 100, 1),
            "silent_periods": self.silent_periods,
            "default_rate_pct": round(self.default_rate_bps / 100, 1),
            "recent_default_rate_pct": round(self.recent_default_rate_bps / 100, 1),
            "avg_gstr3b_delay_days": self.avg_gstr3b_delay_days,
            "worst_gstr3b_delay_days": self.worst_gstr3b_delay_days,
            "registration_status": self.registration_status,
            "trust_score": self.trust_score,
            "pattern": self.pattern,
            "enough_history": self.enough_history,
        }


def profile(history: FilingHistory) -> RiskProfile:
    """One supplier's history, counted."""
    out = RiskProfile(gstin=history.gstin,
                      periods=len(history.months),
                      registration_status=history.registration_status,
                      suspensions=list(history.suspensions))

    delays = []
    recent = history.months[-RECENT_MONTHS:] if history.months else []

    for month in history.months:
        if month.gstr1_filed is not None:
            out.gstr1_filed += 1
        if month.gstr3b_filed is not None:
            out.gstr3b_filed += 1
            delays.append(month.gstr3b_late_days)
        if month.sold_but_did_not_pay:
            out.sold_but_did_not_pay += 1

    for month in recent:
        if month.gstr1_filed is not None:
            out.recent_periods += 1
            if month.sold_but_did_not_pay:
                out.recent_sold_but_did_not_pay += 1

    if delays:
        out.avg_gstr3b_delay_days = sum(delays) // len(delays)
        out.worst_gstr3b_delay_days = max(delays)
    return out


def exposure_at_risk(tax_paise: int, profile: RiskProfile) -> int:
    """
    How much of this month's credit the supplier's own record puts in doubt.

    Their exposure multiplied by the frequency with which they have reported a
    sale and not paid the tax on it. An observed rate, not a forecast - and
    integer arithmetic, so no float ever touches money.
    """
    rate = profile.recent_default_rate_bps if profile.recent_periods \
        else profile.default_rate_bps
    return (abs(tax_paise) * rate) // 10_000


# --- what the drawer needs ------------------------------------------------
#
# Computed here and shipped in the payload rather than worked out in the
# browser. Two reasons, and the second is the real one: client-side date
# arithmetic on a tax deadline would be a second implementation of a statutory
# rule, living somewhere untested, quietly disagreeing with the first.

STATUS_ON_TIME = "on_time"
STATUS_LATE = "late"
STATUS_MISSED = "missed"
STATUS_SILENT = "silent"

STATUS_LABEL = {
    STATUS_ON_TIME: "Filed on time",
    STATUS_LATE: "Filed late",
    STATUS_MISSED: "Reported the sale, never paid the tax",
    STATUS_SILENT: "Filed nothing at all",
}

# A filing this many days past its due date counts as late rather than prompt.
# Not zero: the portal is routinely unreachable on the due date itself.
GRACE_DAYS = 2


def monthly_compliance(history) -> list[dict]:
    """
    Thirty-six months as a grid the drawer can colour in.

    One entry per period, oldest first, each carrying its own status so the
    front end colours cells rather than deciding what they mean.
    """
    out = []
    for month in history.months:
        if month.gstr1_filed is None and month.gstr3b_filed is None:
            status = STATUS_SILENT
        elif month.sold_but_did_not_pay:
            status = STATUS_MISSED
        elif month.gstr3b_late_days > GRACE_DAYS:
            status = STATUS_LATE
        else:
            status = STATUS_ON_TIME

        out.append({
            "period": month.period,
            "status": status,
            "label": STATUS_LABEL[status],
            "gstr1_filed": str(month.gstr1_filed) if month.gstr1_filed else None,
            "gstr3b_filed": (str(month.gstr3b_filed)
                             if month.gstr3b_filed else None),
            "gstr1_late_days": month.gstr1_late_days,
            "gstr3b_late_days": month.gstr3b_late_days,
        })
    return out


def statutory_clocks(invoices: list[dict], today=None) -> dict:
    """
    The two deadlines that decide whether credit survives, per invoice.

        Rule 37   pay the supplier within 180 days of the invoice, or reverse
                  the credit you already took
        s.16(4)   claim by 30 November following the invoice's financial year,
                  after which it is gone

    Both are date arithmetic on the invoice date, and both are computed here
    so the browser never has to know what a financial year is.
    """
    from datetime import date as _date

    today = today or _date.today()
    rows, worst_rule37, soonest_16_4 = [], None, None

    for invoice in invoices:
        raw = invoice.get("invoice_date") or ""
        try:
            when = _date.fromisoformat(str(raw)[:10])
        except (ValueError, TypeError):
            continue

        pay_by = payment_due_by(when)
        claim_by = claim_deadline(when)
        days_to_pay = (pay_by - today).days
        days_to_claim = (claim_by - today).days

        row = {
            "invoice_number": invoice.get("invoice_number", ""),
            "invoice_date": str(when),
            "total_tax": invoice.get("total_tax", 0),
            "rule_37_due": str(pay_by),
            "rule_37_days_left": days_to_pay,
            "rule_37_breached": days_to_pay < 0,
            "days_since_invoice": (today - when).days,
            "claim_deadline": str(claim_by),
            "claim_days_left": days_to_claim,
            "claim_expired": days_to_claim < 0,
        }
        rows.append(row)

        if worst_rule37 is None or days_to_pay < worst_rule37:
            worst_rule37 = days_to_pay
        if soonest_16_4 is None or days_to_claim < soonest_16_4:
            soonest_16_4 = days_to_claim

    breached = [r for r in rows if r["rule_37_breached"]]
    expired = [r for r in rows if r["claim_expired"]]
    return {
        "invoices": rows,
        "rule_37_days_left": worst_rule37,
        "rule_37_breached_count": len(breached),
        "rule_37_breached_tax": sum(r["total_tax"] for r in breached),
        "claim_days_left": soonest_16_4,
        "claim_expired_count": len(expired),
        "claim_expired_tax": sum(r["total_tax"] for r in expired),
        "window_days": SUPPLIER_PAYMENT_DAYS,
    }


# --- what to do about a supplier, decided by arithmetic --------------------
#
# This was the agent's call, and on a borderline record it changed between
# runs: Bright Print House - 100% payment compliance, ten silent months out of
# thirty-six - came back "pay, but keep watching" one afternoon and "safe to
# pay" the next. Both readings were defensible and the reasoning was sound each
# time, which is exactly the problem. A merchant who refreshes and gets
# different advice on unchanged data stops believing the advice.
#
# So the recommendation is a ladder over figures already computed. The agent
# still reads the record, still explains it, and still says when it would go
# further - see agent/risk_agent.py - but it no longer decides.
#
# The thresholds are here to be argued with rather than buried.

ACT_SAFE = "safe_to_pay"
ACT_WATCH = "pay_but_watch"
ACT_HOLD = "hold_payment"
ACT_STOP = "stop_buying"

# How much caution each action represents. Used to tell "the agent agrees" from
# "the agent would go further", which is worth surfacing and is not the same as
# letting it decide.
ACTION_SEVERITY = {
    ACT_SAFE: 0,
    ACT_WATCH: 1,
    "get_it_in_writing": 2,
    ACT_HOLD: 3,
    ACT_STOP: 4,
}

# Default on a quarter of the periods they reported, and their record is no
# longer a wobble.
HOLD_DEFAULT_BPS = 2_500

# Three of every four recent periods reported and unpaid. At that rate the
# question is no longer whether this supplier will regularise - it is whether
# a merchant should keep buying from them at all, because every future invoice
# carries the same problem.
#
# The difference between this rung and hold_payment is about the FUTURE.
# Holding payment is leverage over invoices already raised and is recoverable
# the moment they file. Stopping is a decision about the next order, and it
# needs evidence that the pattern is settled rather than a bad quarter - so it
# reads the recent window, and only where that window is long enough to mean
# something.
STOP_DEFAULT_BPS = 7_500
STOP_MIN_RECENT_PERIODS = 6

# Filed nothing at all in more than a tenth of periods. Not misconduct - they
# may simply have had no sales - but their invoices may not reach GSTR-2B, and
# a merchant should know before assuming the credit will appear.
WATCH_COVERAGE_BPS = 9_000

# Two months late on average. The credit still arrives; it arrives late enough
# to matter for planning, and late enough that a slip could threaten s.16(4).
WATCH_DELAY_DAYS = 60


def recommended_action(profile: "RiskProfile") -> str:
    """
    What to do about this supplier, from their record alone.

    Ordered by what would be worst to get wrong. A dead registration outranks
    everything - credit claimed against one comes back with interest whatever
    the filing history says.
    """
    # Cancelled is not suspended. A suspension can be revoked and the credit
    # recovered; a cancellation means every future invoice from them is
    # unclaimable, and no amount of chasing changes that.
    if profile.registration_status == "cancelled":
        return ACT_STOP
    if (profile.recent_periods >= STOP_MIN_RECENT_PERIODS
            and profile.recent_default_rate_bps >= STOP_DEFAULT_BPS):
        return ACT_STOP
    if not profile.registration_alive:
        return ACT_HOLD
    if not profile.enough_history:
        return ACT_WATCH
    if profile.recent_default_rate_bps >= HOLD_DEFAULT_BPS:
        return ACT_HOLD
    if profile.default_rate_bps >= HOLD_DEFAULT_BPS:
        return ACT_HOLD
    if profile.sold_but_did_not_pay:
        return ACT_WATCH
    if profile.coverage_bps < WATCH_COVERAGE_BPS:
        return ACT_WATCH
    if profile.avg_gstr3b_delay_days >= WATCH_DELAY_DAYS:
        return ACT_WATCH
    return ACT_SAFE
