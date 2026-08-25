"""
Thirty-six months of filing history per supplier, simulated.

## Why this is simulated and what that costs

A supplier's GSTR-1 and GSTR-3B filing dates come from the GST portal, and the
portal reaches third-party software through a GSP - a commercial agreement with
a GSTN-authorised provider, not a weekend signup. So this generates the shape
of that data rather than fetching it, exactly as CLAUDE.md 7.1 does for
settlements: real field names, simulated volume, and say so plainly.

The interface is the honest part. `history_for(gstin)` returns the same
structure a GSP call would, so swapping the source later touches one function.

## The two returns and why the difference matters

    GSTR-1   what a supplier SOLD. Filing it puts the invoice in your GSTR-2B,
             so you can see it and think the credit is yours.
    GSTR-3B  what a supplier PAID. Without this the tax never reached the
             government.

The gap between those two is the entire risk. Under CGST s.16(2)(c), upheld in
Bhandari Scrap Traders, credit exists only where the supplier actually paid -
so a supplier who files GSTR-1 religiously and skips GSTR-3B produces a
purchase register that looks perfect and a tax credit that does not exist. That
supplier is invisible to a reconciliation and is exactly who this is for.

## Statutory due dates

    GSTR-1   11th of the following month
    GSTR-3B  20th of the following month

Late is measured against those, not against a guess.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Optional

GSTR1_DUE_DAY = 11
GSTR3B_DUE_DAY = 20
DEFAULT_MONTHS = 36


class Persona(StrEnum):
    HONEST = "honest_enterprise"
    LATE = "habitual_late_filer"
    DEFAULTER = "gstr3b_defaulter"
    ERRATIC = "erratic"


PERSONA_LABEL = {
    Persona.HONEST: "Files everything, on time",
    Persona.LATE: "Files late, but always files",
    Persona.DEFAULTER: "Reports sales, does not pay the tax",
    Persona.ERRATIC: "Sometimes on time, sometimes not at all",
}

PERSONA_NOTE = {
    Persona.HONEST:
        "The control. Nothing here should ever be flagged.",
    Persona.LATE:
        "Your credit arrives, just later than your books expect. Only a "
        "problem if a delay crosses the s.16(4) deadline.",
    Persona.DEFAULTER:
        "The dangerous one, and invisible to a reconciliation: the invoice "
        "appears in your GSTR-2B because they filed GSTR-1, so everything "
        "looks right - but they never paid, and under s.16(2)(c) the credit "
        "does not exist.",
    Persona.ERRATIC:
        "No pattern. Some months clean, some months nothing at all.",
}


@dataclass
class MonthlyFiling:
    """One tax period for one supplier."""
    period: str                         # "2026-07"
    gstr1_due: date
    gstr1_filed: Optional[date]
    gstr3b_due: date
    gstr3b_filed: Optional[date]

    @property
    def gstr1_late_days(self) -> int:
        if self.gstr1_filed is None:
            return 0
        return max(0, (self.gstr1_filed - self.gstr1_due).days)

    @property
    def gstr3b_late_days(self) -> int:
        if self.gstr3b_filed is None:
            return 0
        return max(0, (self.gstr3b_filed - self.gstr3b_due).days)

    @property
    def sold_but_did_not_pay(self) -> bool:
        """
        Reported the sale and never paid the tax on it.

        The single most important field here. A reconciliation cannot see this
        - the invoice is in GSTR-2B either way.
        """
        return self.gstr1_filed is not None and self.gstr3b_filed is None


@dataclass
class FilingHistory:
    gstin: str
    months: list[MonthlyFiling] = field(default_factory=list)
    registration_status: str = "active"
    suspensions: list[str] = field(default_factory=list)
    persona: Optional[str] = None       # only ever set by the simulator

    def as_rows(self) -> list[dict]:
        """The shape an LLM prompt and a JSON payload both want."""
        return [{
            "period": m.period,
            "gstr1_due": str(m.gstr1_due),
            "gstr1_filed": str(m.gstr1_filed) if m.gstr1_filed else None,
            "gstr1_late_days": m.gstr1_late_days,
            "gstr3b_due": str(m.gstr3b_due),
            "gstr3b_filed": str(m.gstr3b_filed) if m.gstr3b_filed else None,
            "gstr3b_late_days": m.gstr3b_late_days,
            "sold_but_did_not_pay": m.sold_but_did_not_pay,
        } for m in self.months]


def _period(start: date, offset: int) -> tuple[str, date, date]:
    """The period label and the two statutory due dates that follow it."""
    total = start.year * 12 + (start.month - 1) + offset
    year, month = divmod(total, 12)
    month += 1
    due_year, due_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (f"{year}-{month:02d}",
            date(due_year, due_month, GSTR1_DUE_DAY),
            date(due_year, due_month, GSTR3B_DUE_DAY))


def history_for(gstin: str, *, months: int = DEFAULT_MONTHS,
                persona: Optional[Persona] = None,
                ending: Optional[date] = None) -> FilingHistory:
    """
    Thirty-six months for one GSTIN.

    Deterministic from the GSTIN, so the same supplier has the same history
    every time the page is opened - a risk profile that changed on refresh
    would be worse than none.
    """
    rng = random.Random(f"filing:{gstin.strip().upper()}")
    persona = persona or _persona_for(rng)
    ending = ending or date.today()
    start = _months_before(ending, months)

    out = FilingHistory(gstin=gstin.strip().upper(), persona=str(persona))
    for offset in range(months):
        period, gstr1_due, gstr3b_due = _period(start, offset)
        out.months.append(
            _month(persona, rng, period, gstr1_due, gstr3b_due))

    if persona is Persona.DEFAULTER and rng.random() < 0.6:
        # A defaulter often picks up a suspension eventually. Not always - and
        # a merchant cannot rely on the register warning them.
        out.registration_status = "suspended"
        out.suspensions = [out.months[-rng.randint(1, 6)].period]
    return out


def _persona_for(rng: random.Random) -> Persona:
    """Most suppliers are fine. The interesting ones are the minority."""
    return rng.choices(
        [Persona.HONEST, Persona.LATE, Persona.DEFAULTER, Persona.ERRATIC],
        weights=[62, 22, 10, 6])[0]


def _month(persona: Persona, rng: random.Random, period: str,
           gstr1_due: date, gstr3b_due: date) -> MonthlyFiling:
    if persona is Persona.HONEST:
        return MonthlyFiling(period, gstr1_due, gstr1_due - timedelta(days=1),
                             gstr3b_due, gstr3b_due - timedelta(days=1))

    if persona is Persona.LATE:
        # Three to six months late, consistently. Files everything eventually.
        slip = timedelta(days=rng.randint(90, 180))
        return MonthlyFiling(period, gstr1_due, gstr1_due + slip,
                             gstr3b_due, gstr3b_due + slip)

    if persona is Persona.DEFAULTER:
        # Files GSTR-1 - so the buyer sees the invoice and believes the credit
        # is theirs - and skips GSTR-3B most months.
        paid = rng.random() < 0.25
        return MonthlyFiling(
            period, gstr1_due, gstr1_due + timedelta(days=rng.randint(0, 4)),
            gstr3b_due,
            gstr3b_due + timedelta(days=rng.randint(0, 30)) if paid else None)

    roll = rng.random()
    if roll < 0.45:
        return MonthlyFiling(period, gstr1_due, gstr1_due, gstr3b_due,
                             gstr3b_due)
    if roll < 0.8:
        slip = timedelta(days=rng.randint(5, 60))
        return MonthlyFiling(period, gstr1_due, gstr1_due + slip,
                             gstr3b_due, gstr3b_due + slip)
    return MonthlyFiling(period, gstr1_due, None, gstr3b_due, None)


def _months_before(when: date, months: int) -> date:
    total = when.year * 12 + (when.month - 1) - months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)
