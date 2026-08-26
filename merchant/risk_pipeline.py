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
from engine.gst.filing_history import (SOURCE_LABEL, SOURCE_NOTE,
                                       SupplierHistoryService)
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
    # Where this row's filing history came from, and whether the active source
    # had anything at all to say about this supplier. Per-row rather than only
    # per-run: a supplier missing from an uploaded file is scored as unknown,
    # and the row has to be able to say so on its own.
    history_source: str = ""
    history_known: bool = True
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
    # One source per run, never a blend. See SupplierHistoryService for why
    # mixing two sources inside a single table is the failure this prevents.
    history_source: str = ""
    suppliers_without_history: int = 0
    history_failures: list[str] = field(default_factory=list)
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
                # Provenance travels in the payload, not just on the screen.
                # The JSON export is what somebody would hand an auditor, and a
                # trust score without its source on the same page is a figure
                # nobody can calibrate.
                "history_source": self.history_source,
                "history_source_label": SOURCE_LABEL.get(
                    self.history_source, self.history_source),
                "history_source_note": SOURCE_NOTE.get(
                    self.history_source, ""),
                "history_is_demo": self.history_source == "simulated",
                "suppliers_without_history": self.suppliers_without_history,
                "history_failures": list(self.history_failures),
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
        history_source=getattr(history, "source", ""),
        history_known=bool(getattr(history, "known", False)),
        compliance_grid=monthly_compliance(history) if history else [],
        clocks=statutory_clocks([{
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date,
            "total_tax": r.total_tax,
        } for r in group.invoices]))


def run(imported: ImportResult, *, use_agent: bool = True,
        agent=None, months: int = 36,
        history: Optional[SupplierHistoryService] = None,
        on_progress: Optional[Callable[..., None]] = None) -> PortfolioRisk:
    """
    Phases B and C. Phase A already happened in purchase_import.parse.

    `history` decides where the filing records come from - a live API, an
    uploaded file, or the simulator. It changes nothing below this line, which
    is the reason the abstraction exists: one pipeline, one set of arithmetic,
    one output contract, whatever the merchant happened to have access to.
    """
    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    history = history or SupplierHistoryService(months=months)

    out = PortfolioRisk(rows_read=imported.rows_read,
                        rows_skipped=list(imported.rows_skipped),
                        used_agent=use_agent,
                        history_source=history.source)

    say(phase=f"Reading {len(imported.groups)} suppliers' filing history "
              f"({history.label})")

    jobs, rows = [], []
    for group in imported.groups:
        record = history.history_for(group.supplier_gstin)
        if not record.known:
            out.suppliers_without_history += 1
        prof = profile_history(record)
        at_risk = exposure_at_risk(
            group.current_month_total_tax_exposure, prof)
        row = _from_group(group, prof, at_risk, record)
        rows.append(row)
        jobs.append((prof, group.supplier_name,
                     group.current_month_total_tax_exposure, at_risk,
                     record.as_rows()[-RECENT_ROWS:]))

    # A provider that could not be reached collects its failures rather than
    # raising, so one unreachable supplier does not kill a run of fifty. They
    # are surfaced here so the page can say how many, instead of a table of
    # quiet "unknown" rows that look like a verdict.
    out.history_failures = [
        f"{gstin}: {why}"
        for gstin, why in getattr(history.provider, "failures", [])]

    if not use_agent or not jobs:
        out.suppliers = sorted(rows, key=lambda s: (-s.at_risk, s.trust_score))
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


def history_service_for(led, business_id: str, *, months: int = 36,
                        simulated: bool = False,
                        http=None) -> Optional[SupplierHistoryService]:
    """
    Which source a run reads from - or None, meaning refuse.

    `simulated=True` is the Demo Mode tab saying so explicitly. It is a
    parameter rather than something inferred, because a run that quietly
    decided to generate filing records for real companies would be the worst
    thing this product could do, and "the caller asked for it" is the only
    acceptable reason to do it.

    Every other run resolves by evidence, in the order the tabs are laid out:
    a configured API beats uploaded history, because it is more current and
    because it can see payment. Nothing here reads the Razorpay connector -
    which settlement source a business uses says nothing about how it gets
    GST filing history, and keying one off the other was a coupling nobody
    could have predicted from the screen.

    Returning None is a real answer. No API and no uploaded history means
    there is no honest way to score anybody's suppliers, and the caller says
    what is missing instead of inventing it.
    """
    from engine.gst.filing_history import (SimulatedHistoryProvider,
                                           UploadedHistoryProvider)
    from merchant.gstin_lookup import FilingStatusApi, GstinStatus
    from merchant.sources import Sources

    # Registration status is a separate question from filing history and comes
    # from a separate lookup, so it is joined on for every source rather than
    # left to whichever one happens to carry it. Without this a Mode A run
    # reported every supplier as "active", because a returns feed has nothing
    # else to say - and a cancelled registration is the one finding that
    # outranks all the others.
    statuses = {gstin: row["status"] for gstin, row
                in GstinStatus(led.conn).statuses_for(
                    _register_gstins(led)).items()}

    if simulated:
        return SupplierHistoryService(SimulatedHistoryProvider(),
                                      months=months, statuses=statuses)

    config = Sources(led.conn).filing_api_config(business_id)
    if config and config["key_available"]:
        return SupplierHistoryService(
            FilingStatusApi(url_template=config["url_template"],
                            api_key=config["api_key"],
                            key_header=config["key_header"],
                            key_param=config["key_param"], http=http),
            months=months, statuses=statuses)

    held = led.filing_history()
    if held:
        return SupplierHistoryService(
            UploadedHistoryProvider(held), months=months, statuses=statuses)

    return None


def _register_gstins(led) -> list:
    """Every supplier GSTIN this business has bought from."""
    try:
        return [r["supplier_gstin"] for r in led.conn.execute(
            "SELECT DISTINCT supplier_gstin FROM live_purchases"
            " WHERE business_id = ?", (led.business_id,))]
    except Exception:                                       # noqa: BLE001
        return []


NO_HISTORY = (
    "No supplier filing history is available for this business. Upload your "
    "GSTR-2B files on the Without API tab, or connect a GSP on the With API "
    "tab. Scoring real suppliers against generated filing records would not "
    "be a risk assessment, so this run was stopped rather than made up."
)
