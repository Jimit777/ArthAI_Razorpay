"""
Persistence. One SQLite file, no server. CLAUDE.md sections 8 and 9.

Until now everything lived in memory: the pipeline generated a batch, audited
it, printed the result and threw it all away. That was fine for building, and
it is not fine for three things the project actually needs:

  the dashboard has to LOAD a run rather than regenerate one,
  guardrail 5 wants every agent decision timestamped and REPLAYABLE,
  resolution memory (section 12) is by definition a thing that remembers.

The table definitions mirror CLAUDE.md section 9 field for field, and those in
turn mirror Razorpay's settlement recon API. When a real Razorpay export
arrives it should INSERT without translation.

ALL MONEY IS INTEGER PAISE. SQLite INTEGER holds it exactly; a REAL column
would quietly turn Rs 1,627.00 into 1626.9999999999998.

## What this does NOT do

It does not post anything to a ledger. Guardrail 1: the agent proposes, a human
disposes. Every verdict lands here with human_reviewed = 0 and stays that way
until a person says otherwise. The table is a queue of proposals, not a record
of decisions taken.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_PATH = Path(__file__).parent.parent / "auditor.db"

SCHEMA = """
-- One row per execution of the pipeline. Everything else hangs off this, so a
-- file can hold many runs and the dashboard can compare them.
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  seed        INTEGER,
  n_records   INTEGER,
  model       TEXT,
  effort      TEXT,
  via_batch   INTEGER,
  created_at  INTEGER
);

-- From Razorpay Orders/Payments API.
CREATE TABLE IF NOT EXISTS payments (
  run_id           TEXT,
  payment_id       TEXT,
  order_id         TEXT,
  amount           INTEGER,
  currency         TEXT,
  method           TEXT,
  card_network     TEXT,
  card_type        TEXT,
  is_international INTEGER,
  upi_reference    TEXT,
  created_at       INTEGER,
  PRIMARY KEY (run_id, payment_id)
);

-- From Razorpay Settlement Recon API.
CREATE TABLE IF NOT EXISTS settlement_lines (
  run_id        TEXT,
  entity_id     TEXT,
  settlement_id TEXT,
  type          TEXT,
  payment_id    TEXT,
  order_id      TEXT,
  amount        INTEGER,
  fee           INTEGER,
  tax           INTEGER,
  utr           TEXT,
  settled_at    INTEGER,
  PRIMARY KEY (run_id, entity_id)
);

CREATE TABLE IF NOT EXISTS bank_credits (
  run_id      TEXT,
  utr         TEXT,
  amount      INTEGER,
  credited_at INTEGER,
  PRIMARY KEY (run_id, utr)
);

CREATE TABLE IF NOT EXISTS refunds (
  run_id     TEXT,
  refund_id  TEXT,
  payment_id TEXT,
  amount     INTEGER,
  created_at INTEGER,
  PRIMARY KEY (run_id, refund_id)
);

-- Not in section 9's list; rule 10 needs somewhere to live.
CREATE TABLE IF NOT EXISTS tds_entries (
  run_id       TEXT,
  payment_id   TEXT,
  section_code TEXT,
  rate_bps     INTEGER,
  amount       INTEGER,
  deducted_at  INTEGER,
  PRIMARY KEY (run_id, payment_id)
);

-- The merchant's contract, stored with the run so an audit stays reproducible
-- after the rate card is renegotiated.
CREATE TABLE IF NOT EXISTS rate_card (
  run_id     TEXT,
  instrument TEXT,
  rate_bps   INTEGER,
  cap_bps    INTEGER,
  source     TEXT,
  PRIMARY KEY (run_id, instrument)
);

-- The findings. human_reviewed defaults to 0 and nothing in this codebase
-- sets it to 1 - that is a person's job.
CREATE TABLE IF NOT EXISTS variances (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT,
  payment_id      TEXT,
  expected_fee    INTEGER,
  actual_fee      INTEGER,
  expected_tax    INTEGER,
  actual_tax      INTEGER,
  delta           INTEGER,
  money_at_stake  INTEGER,
  exception_code  TEXT,
  confidence      REAL,
  reasoning       TEXT,
  rule_cited      TEXT,
  action          TEXT,
  dispute_text    TEXT,
  decided_by      TEXT,
  queued_for_human INTEGER,
  queue_reasons   TEXT,
  human_reviewed  INTEGER DEFAULT 0,
  created_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_variances_run ON variances(run_id);

-- Guardrail 5. Every agent decision, timestamped and replayable: the evidence
-- it saw, the tools it called, what it answered, what the review corrected,
-- and what it cost. Append-only by convention - nothing here is ever updated.
CREATE TABLE IF NOT EXISTS audit_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           TEXT,
  payment_id       TEXT,
  model            TEXT,
  signals          TEXT,
  tool_calls       TEXT,
  exception_code   TEXT,
  action           TEXT,
  confidence       REAL,
  reasoning        TEXT,
  rule_cited       TEXT,
  evidence_used    TEXT,
  corrections      TEXT,
  invented_figures TEXT,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cache_read_tokens INTEGER,
  latency_ms       INTEGER,
  error            TEXT,
  decided_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);

-- CLAUDE.md section 12. Survives across runs on purpose - that is the point.
--
-- business_id defaults to '' rather than being required: this table is used
-- unscoped by the original single-business command-line tool (merchant/ask.py,
-- merchant/benchmark.py), where there is only ever one business and nothing to
-- scope against. The multi-tenant merchant app always passes a real one - see
-- merchant/ledger.py's migration for a database that predates this column.
CREATE TABLE IF NOT EXISTS resolution_memory (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  exception_code TEXT,
  payment_id     TEXT,
  resolution     TEXT,
  business_id    TEXT DEFAULT '',
  resolved_at    INTEGER
);
"""


# Bytes of randomness after the millisecond prefix.
#
# Three was not enough, and the test that says so was intermittently red
# rather than wrong. Within a single millisecond every id shares its prefix,
# so uniqueness rests entirely on the suffix - and 24 bits gives roughly a
# one-in-a-hundred chance of a collision across 500 ids, which is exactly what
# a loop over seeds generates. A primary key whose docstring promises "unique
# regardless" should not fail once in a hundred runs. Four bytes takes that to
# about three in a hundred thousand and costs two characters.
RUN_ID_BYTES = 4


def new_run_id() -> str:
    """
    Sortable by time, unique regardless.

    A bare millisecond timestamp collided: saving two runs inside the same
    millisecond is not exotic, it is what a loop over seeds does. The random
    suffix costs nothing and the timestamp prefix keeps ids sorting in run order.
    """
    return f"run_{int(time.time() * 1000):x}_{secrets.token_hex(RUN_ID_BYTES)}"


class Store:
    """A thin wrapper over one SQLite file. No ORM, no migrations, no server."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Money must never round. Integers only, and foreign keys on so a
        # variance cannot reference a payment that is not there.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- writing ---------------------------------------------------------

    def save_run(self, batch, run_id: Optional[str] = None, model: str = "",
                 effort: str = "", via_batch: bool = False) -> str:
        """Persist a generated batch. Returns the run_id everything hangs off."""
        run_id = run_id or new_run_id()
        c = self.conn

        c.execute(
            "INSERT INTO runs (run_id, seed, n_records, model, effort, via_batch,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, batch.seed, len(batch.records), model, effort,
             int(via_batch), int(time.time())))

        for r in batch.records:
            p = r.payment
            c.execute(
                "INSERT INTO payments (run_id, payment_id, order_id, amount,"
                " currency, method, card_network, card_type, is_international,"
                " upi_reference, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, p.payment_id, r.order_id, p.amount, "INR", p.method,
                 p.card_network, p.card_type, int(p.is_international),
                 p.upi_reference, r.created_at))

            for ln in r.settlement_lines:
                c.execute(
                    "INSERT INTO settlement_lines (run_id, entity_id,"
                    " settlement_id, type, payment_id, order_id, amount, fee,"
                    " tax, utr, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, ln.entity_id, ln.settlement_id, ln.type,
                     ln.payment_id, ln.order_id, ln.amount, ln.fee, ln.tax,
                     ln.utr, ln.settled_at))

            if r.refund:
                c.execute(
                    "INSERT INTO refunds (run_id, refund_id, payment_id, amount,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (run_id, r.refund.refund_id, r.refund.payment_id,
                     r.refund.amount, r.refund.created_at))

            if r.tds:
                c.execute(
                    "INSERT INTO tds_entries (run_id, payment_id, section_code,"
                    " rate_bps, amount, deducted_at) VALUES (?,?,?,?,?,?)",
                    (run_id, r.tds.payment_id, r.tds.section_code,
                     r.tds.rate_bps, r.tds.amount, r.tds.deducted_at))

        for bc in batch.bank_credits:
            c.execute(
                "INSERT INTO bank_credits (run_id, utr, amount, credited_at)"
                " VALUES (?,?,?,?)",
                (run_id, bc.utr, bc.amount, bc.credited_at))

        for key, spec in batch.rate_card["instruments"].items():
            c.execute(
                "INSERT INTO rate_card (run_id, instrument, rate_bps, cap_bps,"
                " source) VALUES (?,?,?,?,?)",
                (run_id, key,
                 spec["network_mdr_bps"] + spec["platform_fee_bps"],
                 spec.get("network_mdr_cap_bps"), spec["network_mdr_source"]))

        c.commit()
        return run_id

    def save_findings(self, run_id: str, decisions, variances,
                      verdicts: Iterable = (),
                      disputes: Optional[dict] = None) -> None:
        """
        Persist the findings and the audit trail.

        Every row lands with human_reviewed = 0. Nothing in this codebase ever
        sets it to 1: the agent proposes and a person disposes, and a column
        the system can flip itself is not a guardrail.
        """
        by_id = {v.payment_id: v for v in variances}
        now = int(time.time())
        c = self.conn

        for d in decisions:
            v = by_id[d.payment_id]
            c.execute(
                "INSERT INTO variances (run_id, payment_id, expected_fee,"
                " actual_fee, expected_tax, actual_tax, delta, money_at_stake,"
                " exception_code, confidence, reasoning, rule_cited, action,"
                " dispute_text, decided_by, queued_for_human, queue_reasons,"
                " human_reviewed, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (run_id, d.payment_id, v.expected_fee, v.actual_fee,
                 v.expected_tax, v.actual_tax, v.delta, d.money_at_stake,
                 d.exception_code, d.confidence, v.reasoning, v.rule_cited,
                 d.action, (disputes or {}).get(d.payment_id), d.decided_by,
                 int(d.queued_for_human), json.dumps(d.reasons), now))

        for verdict in verdicts:
            v = by_id.get(verdict.payment_id)
            c.execute(
                "INSERT INTO audit_log (run_id, payment_id, model, signals,"
                " tool_calls, exception_code, action, confidence, reasoning,"
                " rule_cited, evidence_used, corrections, invented_figures,"
                " input_tokens, output_tokens, cache_read_tokens, latency_ms,"
                " error, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, verdict.payment_id, verdict.model,
                 json.dumps([asdict(s) for s in v.signals]) if v else "[]",
                 json.dumps(verdict.tool_calls), verdict.exception_code,
                 verdict.action, verdict.confidence, verdict.reasoning,
                 verdict.rule_cited, json.dumps(verdict.evidence_used),
                 json.dumps(verdict.corrections),
                 json.dumps(verdict.invented_figures), verdict.input_tokens,
                 verdict.output_tokens, verdict.cache_read_tokens,
                 verdict.latency_ms, verdict.error, verdict.decided_at))

        # Update the agent reasoning onto the variance rows, where the agent
        # was the one who decided.
        for verdict in verdicts:
            c.execute(
                "UPDATE variances SET reasoning = ?, rule_cited = ?"
                " WHERE run_id = ? AND payment_id = ? AND decided_by = 'agent'",
                (verdict.reasoning, verdict.rule_cited, run_id, verdict.payment_id))

        c.commit()

    def remember_resolution(self, exception_code: str, payment_id: str,
                            resolution: str, business_id: str = "") -> None:
        """
        Record how a human actually resolved a finding. CLAUDE.md section 12.

        `business_id` defaults to '' for the original single-business tool,
        where there is nothing to scope against. The multi-tenant merchant
        app always passes the real one, so one merchant's resolution is never
        recalled for another's variance - see `resolutions()`.
        """
        self.conn.execute(
            "INSERT INTO resolution_memory (exception_code, payment_id,"
            " resolution, business_id, resolved_at) VALUES (?,?,?,?,?)",
            (exception_code, payment_id, resolution, business_id,
             int(time.time())))
        self.conn.commit()

    # human_reviewed is deliberately not settable from here - see
    # test_the_codebase_never_sets_human_reviewed. The engine and agent
    # layers run with no human in the loop, so the capability to flip that
    # column does not exist in either. It lives only in merchant/app.py's
    # /agents/settlement/resolve route, which runs from an actual click.

    # --- reading ---------------------------------------------------------

    def list_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, ("
            "  SELECT COUNT(*) FROM variances v WHERE v.run_id = r.run_id"
            " ) AS findings FROM runs r ORDER BY created_at DESC").fetchall()

    def latest_run_id(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    def findings(self, run_id: str, queued_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM variances WHERE run_id = ?"
        if queued_only:
            sql += " AND queued_for_human = 1"
        return self.conn.execute(sql + " ORDER BY money_at_stake DESC",
                                 (run_id,)).fetchall()

    def audit_trail(self, run_id: str, payment_id: Optional[str] = None):
        if payment_id:
            return self.conn.execute(
                "SELECT * FROM audit_log WHERE run_id = ? AND payment_id = ?"
                " ORDER BY decided_at", (run_id, payment_id)).fetchall()
        return self.conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY decided_at",
            (run_id,)).fetchall()

    def resolutions(self, exception_code: Optional[str] = None,
                    business_id: Optional[str] = None) -> list[sqlite3.Row]:
        """
        Past resolutions, optionally narrowed to one exception code and/or
        one business.

        `business_id=None` (the default) means "don't filter" - which is
        correct for the original single-business tool, where every row
        already belongs to the only business there is. A multi-tenant caller
        MUST pass its actual business_id, or it will recall another
        merchant's resolutions - see merchant/agents/settlement.py, the one
        caller that matters here, which always does.
        """
        clauses, params = [], []
        if exception_code:
            clauses.append("exception_code = ?")
            params.append(exception_code)
        if business_id is not None:
            clauses.append("business_id = ?")
            params.append(business_id)
        sql = "SELECT * FROM resolution_memory"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY resolved_at DESC"
        return self.conn.execute(sql, params).fetchall()

    def totals(self, run_id: str) -> dict:
        """The numbers the dashboard puts at the top of the page."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN queued_for_human THEN 1 ELSE 0 END) AS queued,"
            " SUM(CASE WHEN action = 'dispute' THEN money_at_stake ELSE 0 END)"
            "   AS recoverable_paise,"
            " SUM(CASE WHEN decided_by = 'calculator' THEN 1 ELSE 0 END)"
            "   AS by_calculator"
            " FROM variances WHERE run_id = ?", (run_id,)).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}
