"""
Who read which settlement.

Guardrail 5 already logs what the AGENT decided. This logs what PEOPLE saw -
the other half, and the half a finance product is eventually asked about. "Who
in my company looked at the March settlement?" is a reasonable question and
until now there was no way to answer it.

## The decision that shapes this file

**A log that only records successes misses the interesting event.** Someone
reading their own settlement is routine. Someone trying to reach a business
they are not a member of is the thing you would actually want to know about,
and it is precisely the event a success-only log throws away.

So denials are recorded, with the same detail, and they are what the operator's
platform view shows.

## Append-only, by discipline

SQLite cannot enforce it, so nothing in this codebase writes an UPDATE or a
DELETE against this table and a test greps for both. An audit log that can be
edited is not an audit log; it is a list.

## What is not recorded

Page views that reveal nothing - the agent catalogue, the settings form, the
overview. Logging everything produces a log nobody reads, and a log nobody
reads answers no questions. What is recorded is access to a merchant's actual
money data: a settlement's findings, a dispute letter, a question put to the
agent about their books, and every refusal.

Reading the log is itself logged. A blind spot at the most sensitive page is
where someone would look first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

ACCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          INTEGER NOT NULL,
  user_id     TEXT,
  email       TEXT,
  business_id TEXT,
  action      TEXT NOT NULL,
  target      TEXT,
  outcome     TEXT NOT NULL,      -- 'allowed' | 'denied'
  address     TEXT,
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_biz ON access_log(business_id, at);
CREATE INDEX IF NOT EXISTS idx_access_denied ON access_log(outcome, at);
"""


class Action(StrEnum):
    VIEW_SETTLEMENT = "view_settlement"
    VIEW_DISPUTE = "view_dispute"
    RUN_AUDIT = "run_audit"
    ASK_AGENT = "ask_agent"
    VIEW_ACCESS_LOG = "view_access_log"
    CHANGE_RATE_CARD = "change_rate_card"
    CONNECT_SOURCE = "connect_source"
    SWITCH_BUSINESS = "switch_business"
    CHANGE_ROLE = "change_role"
    ARCHIVE_BUSINESS = "archive_business"
    DELETE_BUSINESS = "delete_business"
    RUN_BENCHMARK = "run_benchmark"
    RESOLVE_FINDING = "resolve_finding"


ACTION_LABEL = {
    Action.VIEW_SETTLEMENT: "Opened a settlement",
    Action.VIEW_DISPUTE: "Read a dispute letter",
    Action.RUN_AUDIT: "Ran the auditor",
    Action.ASK_AGENT: "Asked the agent about the books",
    Action.VIEW_ACCESS_LOG: "Read this access log",
    Action.CHANGE_RATE_CARD: "Changed the rate card",
    Action.CONNECT_SOURCE: "Changed the data source",
    Action.SWITCH_BUSINESS: "Tried to open this business",
    Action.CHANGE_ROLE: "Changed a platform role",
    Action.ARCHIVE_BUSINESS: "Archived or restored a business",
    Action.DELETE_BUSINESS: "Deleted a business",
    Action.RUN_BENCHMARK: "Ran the accuracy benchmark",
    Action.RESOLVE_FINDING: "Marked a settlement finding resolved",
}


@dataclass
class Entry:
    at: int
    email: str
    action: str
    target: Optional[str]
    outcome: str
    address: str
    detail: Optional[str]

    @property
    def denied(self) -> bool:
        return self.outcome == "denied"


class AccessLog:
    """Append-only. Nothing in this class updates or deletes a row."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(ACCESS_SCHEMA)
        self.conn.commit()

    def record(self, action: Action, *, user=None, business_id: Optional[str] = None,
               target: Optional[str] = None, allowed: bool = True,
               address: str = "", detail: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO access_log (at, user_id, email, business_id, action,"
            " target, outcome, address, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(time.time()),
             getattr(user, "user_id", None), getattr(user, "email", None),
             business_id, str(action), target,
             "allowed" if allowed else "denied", address, detail))
        self.conn.commit()

    def denied(self, action: Action, **kw) -> None:
        self.record(action, allowed=False, **kw)

    # --- reading ----------------------------------------------------------

    def for_business(self, business_id: str, limit: int = 100) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT * FROM access_log WHERE business_id = ?"
            " ORDER BY at DESC LIMIT ?", (business_id, limit)).fetchall()
        return [self._entry(r) for r in rows]

    def denials(self, limit: int = 50) -> list:
        """
        Platform-wide refusals, for the operator.

        Refusals only, deliberately. An operator investigating an incident
        needs to know that someone tried to reach a business they are not in.
        They do not need, and are not entitled to, what that business's
        settlements say.
        """
        return self.conn.execute(
            "SELECT * FROM access_log WHERE outcome = 'denied'"
            " ORDER BY at DESC LIMIT ?", (limit,)).fetchall()

    def counts(self, business_id: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) total,"
            " SUM(CASE WHEN outcome = 'denied' THEN 1 ELSE 0 END) denied,"
            " COUNT(DISTINCT user_id) people"
            " FROM access_log WHERE business_id = ?", (business_id,)).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}

    @staticmethod
    def _entry(row) -> Entry:
        return Entry(row["at"], row["email"] or "anonymous", row["action"],
                     row["target"], row["outcome"], row["address"] or "",
                     row["detail"])
