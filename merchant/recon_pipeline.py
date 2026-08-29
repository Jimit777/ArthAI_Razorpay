"""
The whole three-way run: three sources in, a match rate and an exception list
out.

    A  generate or load the three sources                       (generator)
    B  join them in three passes, and compute every gap         (matcher)
    C  ask the agent to explain what is left                    (recon_agent)
    D  score against the answer key and build the payload       (here)

## Why the agent only sees the leftovers

Fifty-five records, six of which are exceptions. Sending the clean fifty to a
model would cost fifty calls to be told fifty times that three numbers agree -
which the arithmetic already established, faster and for nothing. The agent is
asked about the six, which is where judgment is the scarce thing.

That is also what makes the cost defensible. A merchant paying per call should
be paying for the part a script cannot do.

## Why the match rate is measured rather than asserted

The generator returns an answer key, so `accuracy` below compares what the
matcher concluded against what each record was built to be. The number on the
page is therefore checkable, and a matcher that quietly stops finding the hard
cases fails a test rather than producing a slightly worse figure nobody
notices.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from agent.pricing import Usage
from agent.recon_agent import recommended_action
from engine.gst import rules
from engine.recon.matcher import MatchStats, reconcile
from engine.recon.records import (ACTION_LABEL, FINDING_LABEL, FINDING_NOTE,
                                  ReconBatch, ReconRow)


@dataclass
class ReconResult:
    rows: list[ReconRow] = field(default_factory=list)
    stats: MatchStats = field(default_factory=MatchStats)
    processing_time_ms: int = 0
    used_agent: bool = False
    failed_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    total_source_records: int = 0
    accuracy: dict = field(default_factory=dict)

    @property
    def matched(self) -> list[ReconRow]:
        return [r for r in self.rows if r.resolved]

    @property
    def exceptions(self) -> list[ReconRow]:
        return [r for r in self.rows if not r.resolved]

    @property
    def match_rate_bps(self) -> int:
        """Integer basis points. A percentage is derived from this for display,
        never stored, so the figure on the page and the figure in the payload
        cannot drift."""
        if not self.rows:
            return 0
        return (len(self.matched) * 10_000) // len(self.rows)

    @property
    def at_stake(self) -> int:
        return sum(r.at_stake for r in self.exceptions)

    def as_dict(self) -> dict:
        by_finding: dict[str, int] = {}
        for row in self.exceptions:
            by_finding[row.finding] = by_finding.get(row.finding, 0) + 1

        return {
            "metadata": {
                "total_records_processed": self.total_source_records,
                "reconciliation_lines": len(self.rows),
                "processing_time_ms": self.processing_time_ms,
                "judged_by_agent": self.used_agent,
                "failed_calls": self.failed_calls,
                "usage": self.usage.as_dict(),
            },
            "match_metrics": {
                "successful_matches_count": len(self.matched),
                "exception_count": len(self.exceptions),
                "match_rate_percentage": round(self.match_rate_bps / 100, 1),
                "at_stake": self.at_stake,
                "at_stake_display": rules.rupees(self.at_stake),
                "passes": self.stats.as_dict(),
                "exceptions_by_finding": by_finding,
                "accuracy": self.accuracy,
            },
            # Every matched line, with the three ids joined - which is the
            # artefact somebody would actually hand an auditor.
            "matched_records": [{
                "invoice_id": r.invoice.invoice_id if r.invoice else None,
                "txn_id": r.settlement.txn_id if r.settlement else None,
                "utr_number": r.bank.utr_number if r.bank else None,
                "customer_name": r.invoice.customer_name if r.invoice else "",
                "amount": r.settlement.net_settled if r.settlement else 0,
                "amount_display": rules.rupees(
                    r.settlement.net_settled if r.settlement else 0),
                "matched_by": r.matched_by,
                "finding_type": r.finding,
            } for r in self.matched],
            "exception_list": [r.as_dict() for r in self.exceptions],
            "vocabulary": {
                "findings": FINDING_LABEL,
                "notes": FINDING_NOTE,
                "actions": ACTION_LABEL,
            },
        }


def run(batch: ReconBatch, *, truth: Optional[dict] = None,
        use_agent: bool = True, agent=None,
        on_progress: Optional[Callable[..., None]] = None) -> ReconResult:
    """Join the three sources, then ask the agent about what is left."""
    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.monotonic()
    out = ReconResult(used_agent=use_agent,
                      total_source_records=batch.total_records)

    say(phase=f"Reading {len(batch.invoices)} invoices, "
              f"{len(batch.settlements)} settlements and "
              f"{len(batch.bank)} bank credits")

    out.rows, out.stats = reconcile(batch)
    say(phase=f"Joined {len(out.matched)} of {len(out.rows)} lines",
        done=len(out.matched), total=len(out.rows))

    # The recommendation comes from the figures whether or not the agent runs,
    # so a failed call - or no key at all - still leaves a merchant with an
    # actionable exception list rather than an empty column.
    for row in out.rows:
        row.action = recommended_action(row)
        if not row.reasoning:
            row.reasoning = row.detail

    if truth:
        out.accuracy = score(out.rows, truth)

    exceptions = out.exceptions
    if not use_agent or not exceptions:
        out.processing_time_ms = int((time.monotonic() - started) * 1000)
        return out

    say(phase=f"Asking the agent about {len(exceptions)} exceptions")
    if agent is None:
        from agent.recon_agent import ClaudeReconAgent

        agent = ClaudeReconAgent()

    done = {"n": 0}

    def each(_verdict):
        done["n"] += 1
        say(phase=f"Explained {done['n']} of {len(exceptions)}",
            done=done["n"], total=len(exceptions))

    verdicts = {v.key: v for v in agent.judge_all(exceptions, on_each=each)}
    for verdict in verdicts.values():
        out.usage.add(verdict)

    from agent.recon_agent import _key

    for row in exceptions:
        verdict = verdicts.get(_key(row))
        if verdict is None:
            continue
        row.action = verdict.action
        if verdict.headline:
            row.reasoning = verdict.reasoning
        row.tool_calls = list(verdict.tool_calls)
        row.errored = bool(verdict.error)
        if verdict.error:
            out.failed_calls += 1

    out.processing_time_ms = int((time.monotonic() - started) * 1000)
    return out


def score(rows: list[ReconRow], truth: dict) -> dict:
    """
    What the matcher concluded, against what each record was built to be.

    The reason a match rate can be said out loud. Without this the number is
    an assertion; with it, it is a measurement somebody else can repeat.
    """
    from agent.recon_agent import _key

    correct = wrong = 0
    misses: list[dict] = []
    for row in rows:
        key = _key(row)
        expected = truth.get(key)
        if expected is None:
            continue
        if expected == row.finding:
            correct += 1
        else:
            wrong += 1
            misses.append({"key": key, "expected": expected,
                           "got": row.finding})

    checked = correct + wrong
    return {
        "records_with_a_known_answer": checked,
        "correct": correct,
        "wrong": wrong,
        "accuracy_percentage": round(100 * correct / checked, 1) if checked else 0.0,
        "misses": misses[:10],
    }
