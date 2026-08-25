"""
The whole run: file in, portfolio risk out.

    A  parse the register and reduce it to one entry per supplier      (import)
    B  fetch each supplier's filing history, count it, then ask the
       agent what the pattern means                        (risk + risk_agent)
    C  join the judgments back on, multiply, build the payload         (here)

## Where the arithmetic lives

All of it in engine/gst/risk.py, before the agent is called. That ordering is
not incidental: it means a failed model call still leaves a merchant with a
trust score, a pattern and an exposure figure, rather than an empty table. The
agent adds judgment on top of a usable answer; it is not load-bearing for the
numbers.

## Why suppliers are judged one at a time

A hundred suppliers in one prompt is a hundred chances for the model to blend
two of them, and that failure is invisible - a fluent paragraph about the wrong
company. Calls run several at a time instead, which is where the wall-clock
saving actually is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from engine.gst import rules
from engine.gst.filing_history import history_for
from engine.gst.risk import (PATTERN_LABEL, RiskProfile, exposure_at_risk,
                             monthly_compliance,
                             profile as profile_history, recommended_action,
                             statutory_clocks)
from agent.pricing import Usage
from merchant.purchase_import import ImportResult, SupplierGroup

# Above this share of a supplier's credit being in doubt, the row is "high
# risk". A threshold for colour on a table, not for a decision - the decision
# is the agent's, and it is on the row beside this.
HIGH_RISK_BPS = 2_500

RECENT_ROWS = 12


@dataclass
class SupplierRisk:
    """One supplier: what was bought, what their record says, what to do."""
    gstin: str
    supplier_name: str
    invoice_count: int
    taxable_value: int
    exposure: int                       # this month's credit from them, paise
    at_risk: int                        # of that, what their record puts in doubt
    trust_score: int
    pattern: str
    action: str
    headline: str
    reasoning: str
    watch_for: Optional[str] = None
    agent_action: str = ""
    goes_further: bool = False
    registration_status: str = "active"
    profile: dict = field(default_factory=dict)
    invoices: list[dict] = field(default_factory=list)
    # Thirty-six cells for the drawer's grid, and the two statutory deadlines
    # already worked out. Computed here rather than in the browser: date
    # arithmetic on a tax deadline in JavaScript would be a second
    # implementation of a statutory rule, untested and free to disagree with
    # the first.
    compliance_grid: list[dict] = field(default_factory=list)
    clocks: dict = field(default_factory=dict)
    corrections: list[str] = field(default_factory=list)
    errored: bool = False

    @property
    def high_risk(self) -> bool:
        if not self.exposure:
            return False
        return (self.at_risk * 10_000) // self.exposure >= HIGH_RISK_BPS

    def as_dict(self) -> dict:
        out = asdict(self)
        out["high_risk"] = self.high_risk
        out["pattern_label"] = PATTERN_LABEL.get(self.pattern, self.pattern)
        return out


@dataclass
class PortfolioRisk:
    suppliers: list[SupplierRisk] = field(default_factory=list)
    rows_read: int = 0
    rows_skipped: list[str] = field(default_factory=list)
    used_agent: bool = False
    failed_calls: int = 0
    # What the run cost. The verdicts carried this and the pipeline threw it
    # away, so the page could not say what a click had spent - which is the
    # one number a person paying for the API actually wants.
    usage: Usage = field(default_factory=Usage)

    @property
    def total_exposure(self) -> int:
        return sum(s.exposure for s in self.suppliers)

    @property
    def total_at_risk(self) -> int:
        return sum(s.at_risk for s in self.suppliers)

    @property
    def high_risk_exposure(self) -> int:
        return sum(s.exposure for s in self.suppliers if s.high_risk)

    def as_dict(self) -> dict:
        """
        The payload, with invoice-level data nested under each supplier.

        Nested rather than flattened so nothing is lost: a merchant who
        disagrees with a row needs to see which invoices it was built from.
        """
        return {
            "portfolio": {
                "total_pending_itc": self.total_exposure,
                "total_pending_itc_display": rules.rupees(self.total_exposure),
                "itc_at_risk": self.total_at_risk,
                "itc_at_risk_display": rules.rupees(self.total_at_risk),
                "high_risk_exposure": self.high_risk_exposure,
                "high_risk_exposure_display": rules.rupees(
                    self.high_risk_exposure),
                "suppliers": len(self.suppliers),
                "high_risk_suppliers": sum(
                    1 for s in self.suppliers if s.high_risk),
                "rows_read": self.rows_read,
                "rows_skipped": self.rows_skipped,
                "judged_by_agent": self.used_agent,
                "failed_calls": self.failed_calls,
                "usage": self.usage.as_dict(),
            },
            "suppliers": [s.as_dict() for s in self.suppliers],
        }


def _from_group(group: SupplierGroup, prof: RiskProfile,
                at_risk: int, history=None) -> SupplierRisk:
    """The row as the arithmetic alone would produce it, before any judgment."""
    return SupplierRisk(
        gstin=group.supplier_gstin,
        supplier_name=group.supplier_name,
        invoice_count=group.invoice_count,
        taxable_value=group.taxable_value,
        exposure=group.current_month_total_tax_exposure,
        at_risk=at_risk,
        trust_score=prof.trust_score,
        pattern=prof.pattern,
        action=recommended_action(prof),
        headline=f"{group.supplier_name}: "
                 f"{PATTERN_LABEL.get(prof.pattern, prof.pattern).lower()}",
        reasoning="Scored from their filing record. The agent was not asked.",
        registration_status=prof.registration_status,
        profile=prof.as_dict(),
        invoices=[{
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date,
            "taxable_value": r.taxable_value,
            "cgst": r.cgst, "sgst": r.sgst, "igst": r.igst,
            "total_tax": r.total_tax,
        } for r in group.invoices],
        compliance_grid=monthly_compliance(history) if history else [],
        clocks=statutory_clocks([{
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date,
            "total_tax": r.total_tax,
        } for r in group.invoices]))


def run(imported: ImportResult, *, use_agent: bool = True,
        agent=None, months: int = 36,
        on_progress: Optional[Callable[..., None]] = None) -> PortfolioRisk:
    """Phases B and C. Phase A already happened in purchase_import.parse."""
    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    out = PortfolioRisk(rows_read=imported.rows_read,
                        rows_skipped=list(imported.rows_skipped),
                        used_agent=use_agent)

    say(phase=f"Reading {len(imported.groups)} suppliers' filing history")

    jobs, rows = [], []
    for group in imported.groups:
        history = history_for(group.supplier_gstin, months=months)
        prof = profile_history(history)
        at_risk = exposure_at_risk(
            group.current_month_total_tax_exposure, prof)
        row = _from_group(group, prof, at_risk, history)
        rows.append(row)
        jobs.append((prof, group.supplier_name,
                     group.current_month_total_tax_exposure, at_risk,
                     history.as_rows()[-RECENT_ROWS:]))

    if not use_agent or not jobs:
        out.suppliers = rows
        return out

    say(phase=f"Asking the agent about {len(jobs)} suppliers")
    if agent is None:
        from agent.risk_agent import ClaudeRiskAgent

        agent = ClaudeRiskAgent()

    done = {"n": 0}

    def each(_verdict):
        done["n"] += 1
        say(phase=f"Judged {done['n']} of {len(jobs)}",
            done=done["n"], total=len(jobs))

    verdicts = {v.gstin: v for v in agent.judge_all(jobs, on_each=each)}

    for verdict in verdicts.values():
        out.usage.add(verdict)

    for row in rows:
        verdict = verdicts.get(row.gstin)
        if verdict is None:
            continue
        row.pattern = verdict.pattern
        row.action = verdict.action
        row.agent_action = verdict.agent_action
        row.goes_further = verdict.goes_further
        row.headline = verdict.headline
        row.reasoning = verdict.reasoning
        row.watch_for = verdict.watch_for
        row.corrections = list(verdict.corrections)
        row.errored = bool(verdict.error)
        if verdict.error:
            out.failed_calls += 1

    out.suppliers = sorted(rows, key=lambda s: (-s.at_risk, s.trust_score))
    return out
