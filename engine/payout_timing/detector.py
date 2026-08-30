"""
Turn a settlement batch into one summary: how many missed the promised
cycle, and what it cost.

## Why every record here is settled mechanically

Every other detector in this project (CLAUDE.md section 2) settles what it
can and hands the rest to an agent. This one settles everything: a
settlement date is a fact, the promised due date is a fact, and comparing
them is a day-count, not a judgment. There is no hold/reason field anywhere
in this codebase's settlement data (checked directly - `SettlementLine` and
`engine.recon.records.Settlement` carry no such field) that could tell a
"legitimate delay" apart from a genuine miss on any single record, so no
taxonomy code was built around a distinction the evidence can never
resolve. See engine/payout_timing/taxonomy.py.

## Where this hooks into the three-way join

`reconcile()` is called unmodified, with `bank=[]`. Pass 1 (exact
`invoice_reference`) and Pass 2 (windowed amount+date) never touch bank
data - only the credit lookup that runs *after* a pair resolves does, and
with no bank rows it falls through cleanly to `(None, "")`. Every row still
carries real `invoice`/`settlement` objects; only `row.finding` reads as
`MISSING_IN_BANK` for everything, which is simply the wrong vocabulary for
this purpose, not a wrong join - so `row.finding` is ignored entirely here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.payout_timing import rules
from engine.payout_timing.taxonomy import Pattern, PayoutAction, PayoutCode
from engine.recon.matcher import reconcile
from engine.recon.records import ReconBatch


@dataclass
class PayoutRecord:
    invoice_id: str
    txn_id: Optional[str]
    invoice_amount: int
    net_settled: int
    date_issued: date
    due_date: date
    settlement_date: Optional[date]
    delay_working_days: int
    delay_calendar_days: int
    float_cost_paise: int
    code: str


@dataclass
class PayoutTimingSummary:
    records: list[PayoutRecord] = field(default_factory=list)
    n_settled: int = 0
    n_on_time: int = 0
    n_sla_miss: int = 0
    n_unmatched: int = 0
    miss_rate_bps: int = 0
    mean_delay_working_days: float = 0.0     # among misses only
    max_delay_working_days: int = 0
    total_float_cost_paise: int = 0
    worst_offenders: list[PayoutRecord] = field(default_factory=list)
    pattern: str = str(Pattern.CLEAN)
    action: str = str(PayoutAction.NONE)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "n_settled": self.n_settled, "n_on_time": self.n_on_time,
            "n_sla_miss": self.n_sla_miss, "n_unmatched": self.n_unmatched,
            "miss_rate_bps": self.miss_rate_bps,
            "mean_delay_working_days": self.mean_delay_working_days,
            "max_delay_working_days": self.max_delay_working_days,
            "total_float_cost_paise": self.total_float_cost_paise,
            "total_float_cost_display": rules.rupees(self.total_float_cost_paise),
            "pattern": self.pattern, "action": self.action,
            "detail": self.detail,
            "worst_offenders": [
                {"invoice_id": r.invoice_id, "due_date": str(r.due_date),
                 "settlement_date": str(r.settlement_date),
                 "delay_working_days": r.delay_working_days,
                 "float_cost_paise": r.float_cost_paise,
                 "float_cost_display": rules.rupees(r.float_cost_paise)}
                for r in self.worst_offenders],
        }


WORST_OFFENDERS_SHOWN = 5


def detect(batch: ReconBatch, *,
          threshold: rules.PatternThreshold = rules.PatternThreshold()
          ) -> PayoutTimingSummary:
    """One summary for the whole batch - the only unit an agent ever judges."""
    joined, _stats = reconcile(ReconBatch(
        invoices=batch.invoices, settlements=batch.settlements, bank=[]))

    out = PayoutTimingSummary()
    for row in joined:
        if row.invoice is None:
            continue                          # an unclaimed settlement; not
                                               # this agent's concern
        if row.settlement is None:
            out.n_unmatched += 1
            continue

        invoice, settlement = row.invoice, row.settlement
        due = rules.due_date(invoice.date_issued)
        delay_wd = rules.working_days_between(due, settlement.settlement_date)
        delay_cd = (settlement.settlement_date - due).days
        on_time = delay_wd <= 0
        float_cost = rules.float_cost_paise(settlement.net_settled,
                                            max(0, delay_cd))

        record = PayoutRecord(
            invoice_id=invoice.invoice_id, txn_id=settlement.txn_id,
            invoice_amount=invoice.amount, net_settled=settlement.net_settled,
            date_issued=invoice.date_issued, due_date=due,
            settlement_date=settlement.settlement_date,
            delay_working_days=max(0, delay_wd),
            delay_calendar_days=max(0, delay_cd),
            float_cost_paise=float_cost,
            code=str(PayoutCode.ON_TIME if on_time else PayoutCode.SLA_MISS))
        out.records.append(record)
        out.n_settled += 1
        if on_time:
            out.n_on_time += 1
        else:
            out.n_sla_miss += 1
            out.total_float_cost_paise += float_cost

    _summarise(out, threshold)
    return out


def _summarise(out: PayoutTimingSummary, threshold: rules.PatternThreshold
              ) -> None:
    misses = [r for r in out.records if r.code == str(PayoutCode.SLA_MISS)]
    out.miss_rate_bps = (
        (out.n_sla_miss * 10_000) // out.n_settled if out.n_settled else 0)
    out.mean_delay_working_days = (
        sum(r.delay_working_days for r in misses) / len(misses)
        if misses else 0.0)
    out.max_delay_working_days = (
        max((r.delay_working_days for r in misses), default=0))
    out.worst_offenders = sorted(
        misses, key=lambda r: -r.delay_working_days)[:WORST_OFFENDERS_SHOWN]

    systemic = (out.miss_rate_bps >= threshold.systemic_miss_rate_bps
               or out.mean_delay_working_days
               >= threshold.systemic_mean_delay_days)

    if not misses:
        out.pattern = str(Pattern.CLEAN)
        out.action = str(PayoutAction.NONE)
        out.detail = (f"All {out.n_on_time} settled records met the "
                      f"promised cycle. Nothing needs raising.")
    elif systemic:
        out.pattern = str(Pattern.SYSTEMIC_DELAY)
        out.action = str(PayoutAction.ESCALATE)
        out.detail = (
            f"{out.n_sla_miss} of {out.n_settled} settlements "
            f"({out.miss_rate_bps / 100:.1f}%) missed the promised cycle, "
            f"averaging {out.mean_delay_working_days:.1f} working days late "
            f"and costing an assumed {rules.rupees(out.total_float_cost_paise)} "
            f"in float. That rate is systemic, not incidental.")
    else:
        out.pattern = str(Pattern.ISOLATED_DELAY)
        out.action = str(PayoutAction.WATCH)
        out.detail = (
            f"{out.n_sla_miss} of {out.n_settled} settlements missed the "
            f"promised cycle, costing an assumed "
            f"{rules.rupees(out.total_float_cost_paise)} in float. Below "
            f"the rate that calls for raising it, worth watching next period.")
