"""
Thirty-six months of filing history per supplier, from whichever source a
merchant actually has.

## One contract, three sources

Every supplier risk figure in this product is computed from a `FilingHistory`.
Where that history came from changes what a merchant had to do to get it; it
must not change the shape of the answer, the arithmetic applied to it, or the
screen it lands on. So the source sits behind a provider and everything
downstream is identical:

    MODE A  api         a GSP or GST verification API the merchant has a key
                        for. The real thing, and the only one that is current.
    MODE B  file        a filing-history export the merchant assembled - the
                        honest option for anyone without API access.
    MODE C  simulated   deterministic personas, for a demo or a first look.
                        Labelled as such everywhere it appears.

`SupplierHistoryService` picks between them and reports which one is live. The
callers - risk.py, risk_pipeline.py, the drawer - never ask.

## The rule that keeps this honest: absence is not innocence

The temptation with a three-mode design is to paper over gaps: a supplier
missing from an uploaded file quietly gets a simulated history instead, and the
table looks complete. That would be inventing evidence about a real company,
and it is refused here. A supplier the active source knows nothing about gets
an EMPTY history, scores TOO_LITTLE_HISTORY, and says so on the row.

Within Mode B the same distinction is drawn one level finer, because it
matters:

    a row with a blank GSTR-3B cell   an assertion. Someone checked that
                                      period and the return was not filed.
    no row for that period at all     absence of evidence. Nothing is claimed.

The first is counted as a default. The second is not counted at all.

## Why GSTR-2B cannot be a filing-history source

GSTR-2B is a natural thing to reach for - a merchant can download three years
of it themselves, no GSP required. It carries GSTR-1 evidence (the invoice is
there, with the supplier's filing date) and NO GSTR-3B evidence whatsoever.

Feeding that in would set every supplier's GSTR-3B count to zero, which reads
as "reported sales, never paid the tax" - the most serious finding this product
makes - about every supplier in the book. A source that manufactures the worst
possible reading of every company in a merchant's ledger is not a fallback, it
is a defect. So GSTR-2B feeds the reconciliation, which is the question it can
actually answer, and never this.

## Why the simulator still exists

A merchant evaluating this has neither a GSP contract nor three years of filing
exports to hand, and a risk engine with nothing to score cannot be judged. The
simulator produces the shape of that data, exactly as CLAUDE.md 7.1 does for
settlements: real field names, simulated volume, and say so plainly.

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
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Iterable, Optional, Protocol

GSTR1_DUE_DAY = 11
GSTR3B_DUE_DAY = 20
DEFAULT_MONTHS = 36

# Which of the three sources produced a history. Carried on every FilingHistory
# so a figure can always be traced back to what it was computed from - and so
# the UI can say plainly whether a merchant is looking at their own data or at
# a demonstration.
SOURCE_API = "api"
SOURCE_FILE = "file"
SOURCE_SIMULATED = "simulated"
SOURCE_NONE = "none"

SOURCE_LABEL = {
    SOURCE_API: "Connected GST API",
    SOURCE_FILE: "Uploaded filing history",
    SOURCE_SIMULATED: "Simulated history (demo)",
    SOURCE_NONE: "No history available",
}

SOURCE_NOTE = {
    SOURCE_API:
        "Filing dates read per supplier from the API this business has a key "
        "for. Current as of the moment it was queried.",
    SOURCE_FILE:
        "Filing dates read from the history file uploaded for this business. "
        "As current as that file is - nothing here re-checks the portal.",
    SOURCE_SIMULATED:
        "No API key and no uploaded history, so filing records are generated "
        "from deterministic personas. The scores, the arithmetic and the law "
        "are real; the filing dates are not. Do not act on these against a "
        "real supplier.",
    SOURCE_NONE:
        "The active source has no record of this supplier. Nothing is assumed "
        "either way - they score as unknown rather than as clean.",
}


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
    # Which of the three sources this came from, and anything the source wants
    # to say about its own limits. Both travel with the data rather than being
    # inferred later, so a figure can never be shown without its provenance.
    source: str = SOURCE_SIMULATED
    source_note: str = ""

    @property
    def known(self) -> bool:
        """Whether the active source had anything to say about this supplier."""
        return bool(self.months)

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)

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

    out = FilingHistory(gstin=gstin.strip().upper(), persona=str(persona),
                        source=SOURCE_SIMULATED,
                        source_note=SOURCE_NOTE[SOURCE_SIMULATED])
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


# --- the contract every source has to meet ---------------------------------
#
# Everything above this line manufactures history. Everything below it accepts
# history from wherever a merchant actually has it and puts it in the same
# shape, so that risk.py, risk_pipeline.py and the drawer cannot tell the
# difference - which is the entire point of the exercise.

def due_dates(period: str) -> tuple[date, date]:
    """
    The two statutory due dates for a period label like "2026-07".

    Both returns are due in the month AFTER the period they cover, which is the
    detail that makes a naive implementation wrong every December.
    """
    year_text, _, month_text = str(period).strip().partition("-")
    year, month = int(year_text), int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"{period} is not a tax period")
    due_year, due_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(due_year, due_month, GSTR1_DUE_DAY),
            date(due_year, due_month, GSTR3B_DUE_DAY))


def normalise_period(raw) -> Optional[str]:
    """
    A tax period in our shape, from the several ways people write one.

    Accepts "2026-07", "072026" (the GSTN's own ordering), "2026/07", "07-2026"
    and "2026-07-15". Anything else returns None rather than a guess - a period
    read wrongly silently moves a filing into a different month and changes
    whether it was late.
    """
    text = re.sub(r"[^0-9]", "", str(raw or ""))
    if len(text) == 8:                      # a full date; take the month
        text = text[:6] if int(text[:4]) > 1900 else text[2:]
    if len(text) != 6:
        return None
    left, right = text[:4], text[2:]
    # Which half is the year? The GSTN writes MMYYYY, spreadsheets write
    # YYYYMM, and the two are told apart by which end looks like a year.
    if 2000 <= int(left) <= 2100:
        year, month = int(left), int(text[4:])
    elif 2000 <= int(right) <= 2100:
        year, month = int(right), int(text[:2])
    else:
        return None
    if not 1 <= month <= 12:
        return None
    return f"{year}-{month:02d}"


def normalise_date(raw) -> Optional[date]:
    """
    A filing date, from the formats these files actually carry.

    A cell that is present but unparseable returns None, which this module
    treats as "not filed" - so the formats below are deliberately generous, and
    anything outside them is reported by the caller rather than absorbed.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text or text.lower() in {"-", "na", "n/a", "nil", "none",
                                    "not filed", "notfiled", "no"}:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
                    "%d-%b-%Y", "%d %b %Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:11], pattern).date()
        except ValueError:
            continue
    return None


def from_filing_rows(gstin: str, rows: Iterable[dict], *,
                     source: str = SOURCE_FILE,
                     registration_status: str = "active",
                     suspensions: Optional[list[str]] = None,
                     source_note: str = "") -> FilingHistory:
    """
    A supplier's history from period rows, whatever produced them.

    Each row wants `period`, `gstr1_filed` and `gstr3b_filed`; the last two may
    be None, and None means "this period was looked at and the return was not
    filed". That is an assertion, and it is counted as one. A period with no
    row at all never reaches this function and is therefore never counted -
    the distinction the module docstring insists on, enforced here by the fact
    that there is simply nothing to enforce it against.

    Rows arrive in whatever order the file had them and leave oldest first,
    because every consumer - the recent-12 window, the grid, the sparkline -
    assumes chronological order and none of them re-sorts.
    """
    months: dict[str, MonthlyFiling] = {}
    for row in rows:
        period = normalise_period(row.get("period"))
        if period is None:
            continue
        gstr1_due, gstr3b_due = due_dates(period)
        # Last row wins on a duplicated period. Filing history files are often
        # assembled by appending exports, and the later copy is the corrected
        # one more often than not.
        months[period] = MonthlyFiling(
            period=period,
            gstr1_due=gstr1_due,
            gstr1_filed=normalise_date(row.get("gstr1_filed")),
            gstr3b_due=gstr3b_due,
            gstr3b_filed=normalise_date(row.get("gstr3b_filed")))

    return FilingHistory(
        gstin=gstin.strip().upper(),
        months=[months[p] for p in sorted(months)],
        registration_status=registration_status,
        suspensions=list(suspensions or []),
        source=source,
        source_note=source_note or SOURCE_NOTE.get(source, ""))


def blank_history(gstin: str, *, source: str = SOURCE_NONE,
                  note: str = "") -> FilingHistory:
    """
    A supplier the active source knows nothing about.

    Empty rather than simulated, and that is the whole point. An empty history
    scores TOO_LITTLE_HISTORY and recommends pay_but_watch, which is the
    truthful answer to "we have no idea". Substituting invented months here
    would put a confident number under a real company's name.
    """
    return FilingHistory(gstin=gstin.strip().upper(), source=source,
                         source_note=note or SOURCE_NOTE.get(source, ""))


class HistoryProvider(Protocol):
    """One source of filing history. Three implementations, one contract."""

    source: str

    def history_for(self, gstin: str, *, months: int = DEFAULT_MONTHS,
                    ending: Optional[date] = None) -> FilingHistory: ...


class SimulatedHistoryProvider:
    """
    Mode C. Deterministic personas, for a demo or a first look.

    Deterministic from the GSTIN, so the same supplier has the same history
    every time the page is opened - a risk profile that changed on refresh
    would be worse than none.
    """

    source = SOURCE_SIMULATED

    def __init__(self, *, personas: Optional[dict] = None):
        # Only used by tests and the sample register, where a specific mix of
        # behaviours is wanted rather than the weighted draw.
        self._personas = personas or {}

    def history_for(self, gstin: str, *, months: int = DEFAULT_MONTHS,
                    ending: Optional[date] = None) -> FilingHistory:
        return history_for(gstin, months=months, ending=ending,
                           persona=self._personas.get(gstin.strip().upper()))


class UploadedHistoryProvider:
    """
    Mode B. Whatever the merchant assembled and uploaded.

    Holds already-normalised histories keyed by GSTIN. A supplier not in the
    file comes back empty - never simulated, never assumed clean.
    """

    source = SOURCE_FILE

    def __init__(self, histories: dict[str, FilingHistory], *,
                 uploaded_at: Optional[int] = None, filename: str = ""):
        self._histories = {k.strip().upper(): v for k, v in histories.items()}
        self.uploaded_at = uploaded_at
        self.filename = filename

    @property
    def gstins(self) -> list[str]:
        return sorted(self._histories)

    def history_for(self, gstin: str, *, months: int = DEFAULT_MONTHS,
                    ending: Optional[date] = None) -> FilingHistory:
        found = self._histories.get(gstin.strip().upper())
        if found is None:
            return blank_history(
                gstin, source=SOURCE_NONE,
                note="This supplier is not in the uploaded filing history, so "
                     "nothing is known about their returns either way.")
        # Trim to the window the caller asked for, keeping the most recent -
        # a file with sixty months in it should not quietly widen the window
        # the scores were designed around.
        if months and len(found.months) > months:
            return FilingHistory(
                gstin=found.gstin, months=found.months[-months:],
                registration_status=found.registration_status,
                suspensions=list(found.suspensions), source=found.source,
                source_note=found.source_note)
        return found


class ServiceUnavailable(RuntimeError):
    """The configured API could not be reached. Never silently downgraded."""


class SupplierHistoryService:
    """
    Which source is live, and one call to get history from it.

    The resolution order is evidence first: a configured API beats an uploaded
    file beats the simulator, because that is the order of how current the data
    is. Nothing here blends two sources - a run reports one source, and every
    supplier in it was read the same way.

    Mixing would be the subtle failure this design exists to prevent. Half a
    table computed from real filings and half from personas, with no column
    saying which, is worse than either alone: it is a screen a merchant cannot
    calibrate their trust against.
    """

    def __init__(self, provider: Optional[HistoryProvider] = None, *,
                 months: int = DEFAULT_MONTHS):
        self.provider = provider or SimulatedHistoryProvider()
        self.months = months

    @property
    def source(self) -> str:
        return getattr(self.provider, "source", SOURCE_SIMULATED)

    @property
    def label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)

    @property
    def note(self) -> str:
        return SOURCE_NOTE.get(self.source, "")

    @property
    def is_demo(self) -> bool:
        """Whether a merchant is looking at generated data. Said out loud."""
        return self.source == SOURCE_SIMULATED

    def history_for(self, gstin: str, *,
                    ending: Optional[date] = None) -> FilingHistory:
        return self.provider.history_for(gstin, months=self.months,
                                         ending=ending)

    def as_dict(self) -> dict:
        return {"source": self.source, "label": self.label, "note": self.note,
                "is_demo": self.is_demo, "months": self.months}
