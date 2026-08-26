"""
The merchant's live books: orders, captured payments, settlement batches.

Everything downstream of a settlement already exists and is tested - the
detector, the gate, the scoring, the report. What was missing is the part
before it: a merchant taking payments over time, and a gateway settling them in
batches.

## The one design decision worth stating

A settlement batch becomes a RUN in the existing schema. That is why nothing
downstream needed changing: `detect_batch`, `gate_batch`, `score` and
`build_html` all take the same shapes they always did, whether those shapes came
from the synthetic generator or from a person typing into a form. The generator
stops being a special case and becomes one of two ways to produce a settlement.

The Record / SettlementLine / BankCredit dataclasses are imported from the
generator because that is where they were first written, but they are not test
fixtures - they are the domain model, mirroring CLAUDE.md section 9, which
mirrors Razorpay's recon API.

ALL MONEY IS INTEGER PAISE.
"""

from __future__ import annotations

import random
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from engine.expected_value import SETTLEMENT_WORKING_DAYS, add_working_days
from engine.store import Store, new_run_id
from generator.synthetic import (
    Batch,
    BankCredit,
    Payment,
    Record,
    Refund,
    SettlementLine,
    TdsEntry,
    _rzp_id,
    _utr,
)
from merchant.gateway import Behaviour, capture

LIVE_SCHEMA = """
-- Orders the merchant has raised but not necessarily been paid for.
CREATE TABLE IF NOT EXISTS live_orders (
  order_id    TEXT PRIMARY KEY,
  business_id TEXT NOT NULL,
  amount      INTEGER,
  currency    TEXT,
  description TEXT,
  status      TEXT,          -- created | paid
  created_at  INTEGER
);

-- Payments captured against those orders, before any settlement exists.
-- settled_run_id stays NULL until a settlement batch sweeps them up.
CREATE TABLE IF NOT EXISTS live_payments (
  payment_id       TEXT PRIMARY KEY,
  business_id      TEXT NOT NULL,
  order_id         TEXT,
  amount           INTEGER,
  method           TEXT,
  card_network     TEXT,
  card_type        TEXT,
  is_international INTEGER,
  upi_reference    TEXT,
  fee              INTEGER,   -- what the gateway deducted
  tax              INTEGER,   -- and the GST it charged on that
  behaviour        TEXT,      -- how the gateway was configured at capture time
  refunded         INTEGER DEFAULT 0,
  captured_at      INTEGER,
  settled_run_id   TEXT
);

-- Which business a settlement run belongs to.
--
-- A mapping table rather than a column on `runs`, because `runs` lives in
-- engine/store.py and is covered by its own tests. The platform extends the
-- schema without reaching into it.
CREATE TABLE IF NOT EXISTS business_runs (
  run_id      TEXT PRIMARY KEY,
  business_id TEXT NOT NULL,
  created_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_live_payments_biz ON live_payments(business_id);
CREATE INDEX IF NOT EXISTS idx_live_orders_biz   ON live_orders(business_id);

-- --- the other side of the books: what the merchant BUYS -----------------
--
-- The settlement auditor works on sales. Input tax credit works on purchases,
-- and they share nothing but the business they belong to, so they get their
-- own tables rather than columns bolted onto the sales ones.

CREATE TABLE IF NOT EXISTS live_purchases (
  purchase_id     TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  supplier_name   TEXT,
  supplier_gstin  TEXT,
  invoice_number  TEXT,
  invoice_date    TEXT,          -- ISO date; ITC is all about which day
  taxable_value   INTEGER,       -- paise
  cgst            INTEGER,
  sgst            INTEGER,
  igst            INTEGER,
  category        TEXT,          -- set when s.17(5) might bite
  paid_on         TEXT,          -- NULL = supplier still unpaid (Rule 37)
  behaviour       TEXT,          -- how the supplier was set to file
  recorded_at     INTEGER,
  reconciled_run  TEXT
);

-- What the government says suppliers actually reported. In reality this is a
-- JSON file downloaded from the GST portal; here the supplier simulator writes
-- it, which is the same honesty trade CLAUDE.md 7.1 makes for settlements.
CREATE TABLE IF NOT EXISTS live_gstr2b (
  entry_id        TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  supplier_gstin  TEXT,
  invoice_number  TEXT,
  invoice_date    TEXT,
  taxable_value   INTEGER,
  cgst            INTEGER,
  sgst            INTEGER,
  igst            INTEGER,
  filed_period    TEXT,
  recorded_at     INTEGER
);

-- Mode B: filing history a merchant assembled and uploaded, one row per
-- supplier per tax period.
--
-- A NULL filing date here is an ASSERTION, not a gap: the row exists because
-- somebody looked at that period and the return was not filed. A period with
-- no row at all makes no claim either way and is never counted. That
-- distinction is the whole reason this is stored per period rather than as a
-- blob - see engine/gst/filing_history.py, where the same rule is enforced on
-- the way in.
CREATE TABLE IF NOT EXISTS supplier_filing_history (
  business_id     TEXT NOT NULL,
  supplier_gstin  TEXT NOT NULL,
  period          TEXT NOT NULL,     -- 'YYYY-MM'
  gstr1_filed     TEXT,              -- ISO date, or NULL for 'did not file'
  gstr3b_filed    TEXT,
  registration_status TEXT DEFAULT 'active',
  source_file     TEXT,
  uploaded_at     INTEGER,
  PRIMARY KEY (business_id, supplier_gstin, period)
);

CREATE TABLE IF NOT EXISTS business_itc_runs (
  run_id      TEXT PRIMARY KEY,
  business_id TEXT NOT NULL,
  period      TEXT,
  n_invoices  INTEGER,
  created_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_purchases_biz ON live_purchases(business_id);
CREATE INDEX IF NOT EXISTS idx_gstr2b_biz    ON live_gstr2b(business_id);

-- --- watching suppliers over time ---------------------------------------
--
-- A reconciliation answers "what is wrong this month". These tables let the
-- system answer "what CHANGED", which needs a memory of what things looked
-- like last time. Without the snapshot there is nothing to compare against and
-- the watch degrades into a reconciliation that runs on a timer.

CREATE TABLE IF NOT EXISTS watch_checks (
  check_id      TEXT PRIMARY KEY,
  business_id   TEXT NOT NULL,
  at            INTEGER,
  period        TEXT,
  suppliers     INTEGER,
  exposed_paise INTEGER,
  changes_found INTEGER,
  raised        INTEGER,
  used_agent    INTEGER,
  ran_by        TEXT
);

CREATE TABLE IF NOT EXISTS supplier_snapshots (
  check_id                  TEXT,
  business_id               TEXT NOT NULL,
  gstin                     TEXT,
  name                      TEXT,
  invoices_booked           INTEGER,
  invoices_filed            INTEGER,
  tax_booked                INTEGER,
  tax_filed                 INTEGER,
  exposed_paise             INTEGER,
  last_filed_period         TEXT,
  periods_since_filing      INTEGER,
  days_to_earliest_deadline INTEGER,
  status                    TEXT,
  status_changed_on         TEXT,
  PRIMARY KEY (check_id, gstin)
);

-- What the agent decided, including what it decided NOT to raise. Keeping the
-- quiet decisions is the point: "it stayed silent about eleven things" is only
-- a claim you can make if you wrote them down.
CREATE TABLE IF NOT EXISTS watch_raised (
  id               INTEGER PRIMARY KEY,
  check_id         TEXT,
  business_id      TEXT NOT NULL,
  kind             TEXT,
  gstin            TEXT,
  name             TEXT,
  raise_it         INTEGER,
  urgency          TEXT,
  action           TEXT,
  headline         TEXT,
  reasoning        TEXT,
  supplier_message TEXT,
  exposed_paise    INTEGER,
  corrections      TEXT,
  errored          INTEGER DEFAULT 0,
  at               INTEGER
);

CREATE INDEX IF NOT EXISTS idx_watch_checks_biz ON watch_checks(business_id);
CREATE INDEX IF NOT EXISTS idx_watch_raised_biz ON watch_raised(business_id);

-- What the reconciliation concluded about each invoice.
--
-- This did not exist, and its absence is why the reconciliation page was a
-- wall of narration: the run produced findings, narrated them into an
-- in-memory list of sentences, and kept nothing. There was no structured
-- record to build a table from, so the page rendered the sentences.
CREATE TABLE IF NOT EXISTS itc_findings (
  id               INTEGER PRIMARY KEY,
  run_id           TEXT,
  business_id      TEXT NOT NULL,
  invoice_id       TEXT,
  supplier_name    TEXT,
  supplier_gstin   TEXT,
  invoice_number   TEXT,
  invoice_date     TEXT,
  taxable_value    INTEGER,
  cgst             INTEGER,
  sgst             INTEGER,
  igst             INTEGER,
  claimed_tax      INTEGER,
  available_tax    INTEGER,
  delta            INTEGER,
  tolerance        INTEGER,
  exception_code   TEXT,
  action           TEXT,
  confidence       REAL,
  reasoning        TEXT,
  rule_cited       TEXT,
  supplier_message TEXT,
  decided_by       TEXT,
  money_at_stake   INTEGER,
  queued_for_human INTEGER,
  claim_deadline   TEXT,
  days_to_deadline INTEGER,
  evidence         TEXT,          -- JSON: the signals, for "show the working"
  created_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_itc_findings_run ON itc_findings(run_id);
"""





class Ledger:
    """
    One business's books.

    Every query is scoped to `business_id`. That scoping is the only thing
    standing between two merchants' data in a shared file, so it is tested
    directly rather than assumed - see test_merchant.py.
    """

    def __init__(self, path, business_id: Optional[str] = None):
        self.store = Store(path)
        self.store.conn.executescript(LIVE_SCHEMA)
        self.store.conn.commit()
        from merchant.businesses import Businesses

        self.businesses = Businesses(self.store.conn)
        self.business_id = business_id
        self._rng = random.Random()

    def _scoped(self) -> str:
        if not self.business_id:
            raise ValueError("this operation needs a business; none is selected")
        return self.business_id

    def close(self) -> None:
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def conn(self):
        return self.store.conn

    # --- how the gateway is behaving -------------------------------------

    def behaviour(self) -> Behaviour:
        return Behaviour(self.businesses.behaviour(self._scoped()))

    def set_behaviour(self, behaviour: Behaviour) -> None:
        self.businesses.set_behaviour(self._scoped(), str(behaviour))

    def rate_card(self) -> dict:
        """This business's contract, not a shared file."""
        return self.businesses.rate_card(self._scoped())



    # --- watching suppliers over time -------------------------------------

    def last_check(self):
        return self.conn.execute(
            "SELECT * FROM watch_checks WHERE business_id = ?"
            # Whole-second timestamps tie when two checks run a moment apart,
            # and a tie here means "the last check" is whichever row SQLite
            # returned first. rowid breaks it by insertion order.
            " ORDER BY at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    def record_itc_findings(self, run_id: str, variances, decisions,
                            verdicts=None) -> None:
        """
        Store what the reconciliation concluded, invoice by invoice.

        Everything the page needs is written here, including the evidence the
        detector produced - so "show the working" is a stored fact rather than
        something reconstructed later from a different code path and quietly
        drifting away from what the merchant was actually told.
        """
        import json
        import time

        by_id = {v.invoice_id: v for v in variances}
        verdict_by_id = {v.invoice_id: v for v in (verdicts or [])}
        now = int(time.time())
        rows = []

        for decision in decisions:
            variance = by_id.get(decision.invoice_id)
            if variance is None:
                continue
            verdict = verdict_by_id.get(decision.invoice_id)
            evidence = json.dumps([{
                "kind": s.kind, "detail": s.detail, "rule": s.rule,
                "source": s.source, "amount_paise": s.amount_paise,
            } for s in variance.signals])
            rows.append((
                run_id, self._scoped(), variance.invoice_id,
                variance.supplier_name, variance.supplier_gstin,
                variance.invoice_number, str(variance.invoice_date),
                variance.raw.get("taxable_value", 0), variance.raw.get("cgst", 0),
                variance.raw.get("sgst", 0), variance.raw.get("igst", 0),
                variance.claimed_tax, variance.available_tax, variance.delta,
                variance.tolerance, decision.exception_code, decision.action,
                decision.confidence,
                (verdict.reasoning if verdict else variance.reasoning) or "",
                (verdict.rule_cited if verdict else variance.rule_cited) or "",
                verdict.supplier_message if verdict else None,
                decision.decided_by, decision.money_at_stake,
                int(decision.queued_for_human),
                variance.raw.get("claim_deadline"), variance.days_to_deadline,
                evidence, now))

        self.conn.executemany(
            "INSERT INTO itc_findings (run_id, business_id, invoice_id,"
            " supplier_name, supplier_gstin, invoice_number, invoice_date,"
            " taxable_value, cgst, sgst, igst, claimed_tax, available_tax,"
            " delta, tolerance, exception_code, action, confidence, reasoning,"
            " rule_cited, supplier_message, decided_by, money_at_stake,"
            " queued_for_human, claim_deadline, days_to_deadline, evidence,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?)", rows)
        self.conn.commit()

    def itc_findings(self, run_id: str, needing_action: bool = False) -> list:
        from engine.gst.taxonomy import NO_ACTION

        sql = ("SELECT * FROM itc_findings WHERE business_id = ? AND run_id = ?")
        params = [self._scoped(), run_id]
        if needing_action:
            quiet = ",".join("?" * len(NO_ACTION))
            sql += f" AND exception_code NOT IN ({quiet})"
            params += [str(c) for c in NO_ACTION]
        return self.conn.execute(
            sql + " ORDER BY money_at_stake DESC", params).fetchall()

    def latest_itc_run(self):
        return self.conn.execute(
            "SELECT * FROM business_itc_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    def watch_checks(self, limit: int = 20) -> list:
        return self.conn.execute(
            "SELECT * FROM watch_checks WHERE business_id = ?"
            " ORDER BY at DESC, rowid DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    def last_snapshot(self) -> dict:
        """
        The supplier picture as of the previous check, rebuilt.

        Empty on the first ever check, which is correct and is why the first
        run reports nothing as "changed" - everything is simply new.
        """
        from engine.gst.watch import SupplierState

        previous = self.last_check()
        if previous is None:
            return {}
        rows = self.conn.execute(
            "SELECT * FROM supplier_snapshots WHERE check_id = ?",
            (previous["check_id"],)).fetchall()
        return {r["gstin"]: SupplierState(
            gstin=r["gstin"], name=r["name"],
            invoices_booked=r["invoices_booked"],
            invoices_filed=r["invoices_filed"],
            tax_booked=r["tax_booked"], tax_filed=r["tax_filed"],
            exposed_paise=r["exposed_paise"],
            last_filed_period=r["last_filed_period"],
            periods_since_filing=r["periods_since_filing"],
            days_to_earliest_deadline=r["days_to_earliest_deadline"],
            status=r["status"], status_changed_on=r["status_changed_on"])
            for r in rows}

    def record_check(self, states: dict, raised: list, *, period: str,
                     used_agent: bool, ran_by: str = "") -> str:
        import json
        import time

        check_id = f"chk_{secrets.token_hex(6)}"
        business_id = self._scoped()
        now = int(time.time())
        exposed = sum(s.exposed_paise for s in states.values())

        self.conn.execute(
            "INSERT INTO watch_checks (check_id, business_id, at, period,"
            " suppliers, exposed_paise, changes_found, raised, used_agent,"
            " ran_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (check_id, business_id, now, period, len(states), exposed,
             len(raised), sum(1 for r in raised if r.raise_it),
             int(used_agent), ran_by))

        self.conn.executemany(
            "INSERT INTO supplier_snapshots (check_id, business_id, gstin,"
            " name, invoices_booked, invoices_filed, tax_booked, tax_filed,"
            " exposed_paise, last_filed_period, periods_since_filing,"
            " days_to_earliest_deadline, status, status_changed_on)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(check_id, business_id, s.gstin, s.name, s.invoices_booked,
              s.invoices_filed, s.tax_booked, s.tax_filed, s.exposed_paise,
              s.last_filed_period, s.periods_since_filing,
              s.days_to_earliest_deadline, s.status, s.status_changed_on)
             for s in states.values()])

        self.conn.executemany(
            "INSERT INTO watch_raised (check_id, business_id, kind, gstin,"
            " name, raise_it, urgency, action, headline, reasoning,"
            " supplier_message, exposed_paise, corrections, errored, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(check_id, business_id, r.kind, r.gstin, r.name, int(r.raise_it),
              r.urgency, r.action, r.headline, r.reasoning,
              r.supplier_message, r.exposed_paise,
              json.dumps(r.corrections), int(bool(r.error)), now)
             for r in raised])
        self.conn.commit()
        return check_id

    # Urgency outranks amount, and the ordering has to say so.
    #
    # Sorting by rupees put "this week" above "do this now" on the page, which
    # is precisely the judgment the agent had just made and the page then threw
    # away. A cancelled registration accruing interest at 18% outranks a larger
    # sum with a year left to claim - that is the whole reason there is an
    # agent here rather than a threshold.
    URGENCY_ORDER = "CASE urgency WHEN 'now' THEN 0 WHEN 'this_week' THEN 1" \
                    " WHEN 'this_month' THEN 2 ELSE 3 END"

    def raised_in(self, check_id: str, only_raised: bool = True) -> list:
        clause = " AND raise_it = 1" if only_raised else ""
        return self.conn.execute(
            f"SELECT * FROM watch_raised WHERE business_id = ?"
            f" AND check_id = ?{clause}"
            f" ORDER BY {self.URGENCY_ORDER}, exposed_paise DESC",
            (self._scoped(), check_id)).fetchall()

    def supplier_register(self, check_id: Optional[str] = None) -> list:
        """Suppliers ranked by how much of the merchant's money they hold."""
        if check_id is None:
            previous = self.last_check()
            if previous is None:
                return []
            check_id = previous["check_id"]
        return self.conn.execute(
            "SELECT * FROM supplier_snapshots WHERE business_id = ?"
            " AND check_id = ? ORDER BY exposed_paise DESC, name",
            (self._scoped(), check_id)).fetchall()

    # --- purchases, and what suppliers reported about them ----------------

    def record_purchase(self, *, supplier_name: str, taxable_value: int,
                        rate_bps: int = 1800, interstate: bool = False,
                        behaviour=None, category: Optional[str] = None,
                        invoice_date=None, paid: bool = True,
                        invoice_number: Optional[str] = None) -> str:
        """
        Book a purchase invoice, and let the supplier file (or not) at once.

        Deliberately mirrors capture_payment: the counterparty's behaviour is
        decided at the moment the record is created, not at audit time. The
        auditor must never be able to influence what it is auditing.
        """
        import time

        from merchant.suppliers import (SupplierBehaviour, current_period,
                                        file_invoice, gstin_for, split_tax)

        business_id = self._scoped()
        # Whatever the simulator is set to, unless a caller names one - the
        # generator and the tests still need to plant a specific fault.
        behaviour = SupplierBehaviour(
            behaviour or self.businesses.supplier_behaviour(business_id))
        when = invoice_date or date.today()
        state = "24" if interstate else "27"
        gstin = gstin_for(supplier_name, state)
        number = invoice_number or f"{supplier_name[:3].upper()}/{self._rng.randint(1000, 9999)}"
        cgst, sgst, igst = split_tax(taxable_value, rate_bps, interstate)
        purchase_id = f"pur_{secrets.token_hex(6)}"
        period = current_period(when)

        self.conn.execute(
            "INSERT INTO live_purchases (purchase_id, business_id,"
            " supplier_name, supplier_gstin, invoice_number, invoice_date,"
            " taxable_value, cgst, sgst, igst, category, paid_on, behaviour,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (purchase_id, business_id, supplier_name, gstin, number, str(when),
             taxable_value, cgst, sgst, igst, category,
             str(when) if paid else None, str(behaviour), int(time.time())))

        filed = file_invoice(
            supplier_gstin=gstin, invoice_number=number, invoice_date=when,
            taxable_value=taxable_value, cgst=cgst, sgst=sgst, igst=igst,
            period=period, behaviour=behaviour)
        if filed is not None:
            self.conn.execute(
                "INSERT INTO live_gstr2b (entry_id, business_id,"
                " supplier_gstin, invoice_number, invoice_date, taxable_value,"
                " cgst, sgst, igst, filed_period, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"2b_{secrets.token_hex(6)}", business_id,
                 filed["supplier_gstin"], filed["invoice_number"],
                 str(filed["invoice_date"]), filed["taxable_value"],
                 filed["cgst"], filed["sgst"], filed["igst"],
                 filed["filed_period"], int(time.time())))
        self.conn.commit()
        return purchase_id

    def replace_purchase_register(self, imported) -> dict:
        """
        Store an uploaded register, replacing whatever was held before.

        Replacing rather than appending: a register is a statement of a
        period's purchases, and a merchant who uploads a corrected export
        expects it to correct things rather than double them. Anything already
        reconciled keeps its run id so the findings still point at real rows.
        """
        import time

        business_id = self._scoped()
        removed = self.conn.execute(
            "DELETE FROM live_purchases WHERE business_id = ?"
            " AND reconciled_run IS NULL", (business_id,)).rowcount

        now = int(time.time())
        rows = []
        for group in imported.groups:
            for invoice in group.invoices:
                rows.append((
                    f"pur_{secrets.token_hex(6)}", business_id,
                    group.supplier_name, group.supplier_gstin,
                    invoice.invoice_number, invoice.invoice_date,
                    invoice.taxable_value, invoice.cgst, invoice.sgst,
                    invoice.igst, None, None, "imported", now))

        self.conn.executemany(
            "INSERT INTO live_purchases (purchase_id, business_id,"
            " supplier_name, supplier_gstin, invoice_number, invoice_date,"
            " taxable_value, cgst, sgst, igst, category, paid_on, behaviour,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return {"added": len(rows), "removed": removed}

    # --- mode B storage ---------------------------------------------------

    def replace_filing_history(self, imported) -> dict:
        """
        Store an uploaded filing history, replacing what was held before.

        Replacing wholesale rather than merging per period. A merchant who
        re-uploads is correcting the file, and a merge would leave rows from
        the old one alive underneath - which for this table means a period
        someone deliberately removed silently keeps counting as a default.
        """
        import time

        business_id = self._scoped()
        self.conn.execute(
            "DELETE FROM supplier_filing_history WHERE business_id = ?",
            (business_id,))

        now = int(time.time())
        rows = []
        for gstin, history in imported.histories.items():
            for month in history.months:
                rows.append((
                    business_id, gstin, month.period,
                    month.gstr1_filed.isoformat() if month.gstr1_filed else None,
                    month.gstr3b_filed.isoformat() if month.gstr3b_filed else None,
                    history.registration_status, imported.filename, now))

        self.conn.executemany(
            "INSERT INTO supplier_filing_history (business_id, supplier_gstin,"
            " period, gstr1_filed, gstr3b_filed, registration_status,"
            " source_file, uploaded_at) VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return {"suppliers": len(imported.histories), "periods": len(rows)}

    def filing_history(self) -> dict:
        """
        Everything held for this business, in the standard contract.

        Returns GSTIN -> FilingHistory, ready to hand to
        UploadedHistoryProvider. Empty means mode B is not available and the
        caller should fall back - which it decides, not this.
        """
        from engine.gst.filing_history import SOURCE_FILE, from_filing_rows

        grouped: dict[str, list[dict]] = {}
        statuses: dict[str, str] = {}
        for row in self.conn.execute(
                "SELECT * FROM supplier_filing_history WHERE business_id = ?"
                " ORDER BY supplier_gstin, period", (self._scoped(),)):
            grouped.setdefault(row["supplier_gstin"], []).append({
                "period": row["period"],
                "gstr1_filed": row["gstr1_filed"],
                "gstr3b_filed": row["gstr3b_filed"]})
            statuses[row["supplier_gstin"]] = (
                row["registration_status"] or "active")

        return {gstin: from_filing_rows(
                    gstin, rows, source=SOURCE_FILE,
                    registration_status=statuses.get(gstin, "active"))
                for gstin, rows in grouped.items()}

    def filing_history_summary(self) -> dict:
        """What the page needs to describe the upload without loading it all."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT supplier_gstin) AS suppliers,"
            " COUNT(*) AS periods, MIN(period) AS first_period,"
            " MAX(period) AS last_period, MAX(uploaded_at) AS uploaded_at,"
            " MAX(source_file) AS source_file"
            " FROM supplier_filing_history WHERE business_id = ?",
            (self._scoped(),)).fetchone()
        if row is None or not row["suppliers"]:
            return {}
        return dict(row)

    def forget_filing_history(self) -> int:
        removed = self.conn.execute(
            "DELETE FROM supplier_filing_history WHERE business_id = ?",
            (self._scoped(),)).rowcount
        self.conn.commit()
        return removed

    def import_gstr2b(self, lines, period: str = "") -> dict:
        """
        Store one period's GSTR-2B lines, replacing anything already held for it.

        Replacing rather than appending: a merchant who downloads July twice
        should end up with July once. GSTR-2B is a static statement - it does
        not change after generation - so a second copy of the same period is a
        duplicate, never an update.
        """
        import time

        business_id = self._scoped()
        replaced = 0
        if period:
            replaced = self.conn.execute(
                "DELETE FROM live_gstr2b WHERE business_id = ?"
                " AND filed_period = ?", (business_id, period)).rowcount

        now = int(time.time())
        self.conn.executemany(
            "INSERT INTO live_gstr2b (entry_id, business_id, supplier_gstin,"
            " invoice_number, invoice_date, taxable_value, cgst, sgst, igst,"
            " filed_period, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(f"2b_{secrets.token_hex(6)}", business_id, l.supplier_gstin,
              l.invoice_number, str(l.invoice_date) if l.invoice_date else "",
              l.taxable_value, l.cgst, l.sgst, l.igst,
              l.filed_period or period, now) for l in lines])
        self.conn.commit()
        return {"period": period, "added": len(lines), "replaced": replaced}

    def gstr2b_periods(self) -> list:
        """Which periods have been imported, and how much each holds."""
        return self.conn.execute(
            "SELECT filed_period, COUNT(*) n,"
            " COALESCE(SUM(cgst + sgst + igst), 0) tax,"
            " COUNT(DISTINCT supplier_gstin) suppliers"
            " FROM live_gstr2b WHERE business_id = ?"
            " GROUP BY filed_period ORDER BY filed_period DESC",
            (self._scoped(),)).fetchall()

    def record_zoho_purchase(self, purchase: dict) -> str:
        """
        Record a purchase that came out of the merchant's own accounting system.

        Deliberately NOT record_purchase. That one asks the supplier simulator
        what to file, which is right for demo data and completely wrong here -
        a real bill's filing status comes from a real GSTR-2B, and inventing
        one would mean the reconciler grading data we made up about a supplier
        who actually exists.
        """
        import time

        business_id = self._scoped()
        purchase_id = f"pur_{secrets.token_hex(6)}"
        self.conn.execute(
            "INSERT INTO live_purchases (purchase_id, business_id,"
            " supplier_name, supplier_gstin, invoice_number, invoice_date,"
            " taxable_value, cgst, sgst, igst, category, paid_on, behaviour,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (purchase_id, business_id, purchase["supplier_name"],
             purchase["supplier_gstin"], purchase["invoice_number"],
             purchase["invoice_date"], purchase["taxable_value"],
             purchase["cgst"], purchase["sgst"], purchase["igst"],
             None, purchase.get("paid_on"), "imported", int(time.time())))
        self.conn.commit()
        return purchase_id

    def purchases(self, limit: int = 50) -> list:
        return self.conn.execute(
            "SELECT * FROM live_purchases WHERE business_id = ?"
            " ORDER BY recorded_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    def unreconciled_purchases(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_purchases WHERE business_id = ?"
            " AND reconciled_run IS NULL ORDER BY recorded_at",
            (self._scoped(),)).fetchall()

    def build_itc_batch(self, only_unreconciled: bool = True):
        """
        Assemble the merchant's real purchases into the shape the ITC engine
        already takes, so nothing in engine/gst had to change to run on live
        data rather than on a generated batch.
        """
        from engine.gst.generator import GSTR2BLine, ITCBatch, PurchaseInvoice
        from merchant.suppliers import current_period

        rows = (self.unreconciled_purchases() if only_unreconciled
                else self.purchases(limit=1_000))
        if not rows:
            return None

        def as_date(text):
            return date.fromisoformat(text) if text else None

        purchases = [PurchaseInvoice(
            invoice_id=r["purchase_id"], supplier_name=r["supplier_name"],
            supplier_gstin=r["supplier_gstin"],
            invoice_number=r["invoice_number"],
            invoice_date=as_date(r["invoice_date"]),
            taxable_value=r["taxable_value"], cgst=r["cgst"], sgst=r["sgst"],
            igst=r["igst"], category=r["category"],
            paid_on=as_date(r["paid_on"])) for r in rows]

        filed = self.conn.execute(
            "SELECT * FROM live_gstr2b WHERE business_id = ?",
            (self._scoped(),)).fetchall()
        gstr2b = [GSTR2BLine(
            supplier_gstin=f["supplier_gstin"],
            invoice_number=f["invoice_number"],
            invoice_date=as_date(f["invoice_date"]),
            taxable_value=f["taxable_value"], cgst=f["cgst"], sgst=f["sgst"],
            igst=f["igst"], filed_period=f["filed_period"]) for f in filed]

        return ITCBatch(purchases=purchases, gstr2b=gstr2b,
                        as_of=date.today(), period=current_period())

    def commit_itc_run(self, batch, period: str = "") -> str:
        import time

        from merchant.suppliers import current_period

        run_id = f"itc_{secrets.token_hex(6)}"
        business_id = self._scoped()
        self.conn.execute(
            "INSERT INTO business_itc_runs (run_id, business_id, period,"
            " n_invoices, created_at) VALUES (?,?,?,?,?)",
            (run_id, business_id, period or current_period(),
             len(batch.purchases), int(time.time())))
        self.conn.executemany(
            "UPDATE live_purchases SET reconciled_run = ? WHERE purchase_id = ?",
            [(run_id, p.invoice_id) for p in batch.purchases])
        self.conn.commit()
        return run_id

    def itc_runs(self) -> list:
        return self.conn.execute(
            "SELECT * FROM business_itc_runs WHERE business_id = ?"
            " ORDER BY created_at DESC", (self._scoped(),)).fetchall()

    def owns_itc_run(self, run_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM business_itc_runs WHERE run_id = ? AND business_id = ?",
            (run_id, self._scoped())).fetchone() is not None

    def purchases_in_run(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM live_purchases WHERE business_id = ?"
            " AND reconciled_run = ? ORDER BY recorded_at",
            (self._scoped(), run_id)).fetchall()

    # --- the journey ------------------------------------------------------

    def create_order(self, amount: int, description: str = "") -> str:
        order_id = _rzp_id(self._rng, "order_")
        self.conn.execute(
            "INSERT INTO live_orders (order_id, business_id, amount, currency,"
            " description, status, created_at) VALUES (?,?,?,?,?,'created',?)",
            (order_id, self._scoped(), amount, "INR", description, int(time.time())))
        self.conn.commit()
        return order_id

    def capture_payment(self, order_id: str, method: str,
                        card_network: Optional[str] = None,
                        card_type: Optional[str] = None,
                        is_international: bool = False) -> str:
        """
        Take the money. The gateway decides its own fee; we record what it did.

        Note what is NOT happening here: nothing checks whether the fee is
        correct. That is the auditor's job and it happens after settlement,
        which is exactly the problem the product exists for - the merchant
        cannot see the error at the moment it is made.
        """
        order = self.conn.execute(
            "SELECT * FROM live_orders WHERE order_id = ? AND business_id = ?",
            (order_id, self._scoped())).fetchone()
        if order is None:
            raise KeyError(f"no order {order_id} for this business")

        result = capture(order["amount"], method, self.behaviour(),
                         card_network, card_type, is_international, self._rng)
        payment_id = _rzp_id(self._rng, "pay_")

        self.conn.execute(
            "INSERT INTO live_payments (payment_id, business_id, order_id, amount,"
            " method, card_network, card_type, is_international, upi_reference,"
            " fee, tax, behaviour, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payment_id, self._scoped(), order_id, order["amount"], result.method,
             result.card_network, result.card_type, int(is_international),
             result.upi_reference, result.fee, result.tax,
             str(self.behaviour()), int(time.time())))
        self.conn.execute("UPDATE live_orders SET status='paid' WHERE order_id = ?",
                          (order_id,))
        self.conn.commit()
        return payment_id

    def payment(self, payment_id: str):
        return self.conn.execute(
            "SELECT * FROM live_payments WHERE payment_id = ? AND business_id = ?",
            (payment_id, self._scoped())).fetchone()

    def refund_payment(self, payment_id: str) -> None:
        """
        Refund in full. The fee is NOT reversed - rule 8, and the reason the
        auditor has to know the difference between a retained fee and an
        overcharge.
        """
        self.conn.execute(
            "UPDATE live_payments SET refunded = 1"
            " WHERE payment_id = ? AND business_id = ?",
            (payment_id, self._scoped()))
        self.conn.commit()

    def unsettled(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_payments WHERE settled_run_id IS NULL"
            " AND business_id = ? ORDER BY captured_at", (self._scoped(),)).fetchall()

    def orders(self, limit: int = 50) -> list:
        return self.conn.execute(
            "SELECT o.*, p.payment_id, p.fee, p.tax, p.method AS paid_method,"
            " p.refunded, p.settled_run_id"
            " FROM live_orders o LEFT JOIN live_payments p"
            " ON p.order_id = o.order_id WHERE o.business_id = ?"
            " ORDER BY o.created_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    # --- settlement -------------------------------------------------------

    def build_settlement(self, rate_card: dict) -> Optional[Batch]:
        """
        Sweep every unsettled payment into one batch, exactly as a gateway does.

        Returns the same Batch shape the synthetic generator returns, so the
        whole audit pipeline downstream is unchanged. Returns None when there is
        nothing to settle.
        """
        pending = self.unsettled()
        if not pending:
            return None

        settlement_id = _rzp_id(self._rng, "setl_")
        utr = _utr(self._rng)
        settled_at = int(add_working_days(
            datetime.now(timezone.utc), SETTLEMENT_WORKING_DAYS).timestamp())

        records: list[Record] = []
        for row in pending:
            payment = Payment(
                payment_id=row["payment_id"], amount=row["amount"],
                method=row["method"], card_network=row["card_network"],
                card_type=row["card_type"],
                is_international=bool(row["is_international"]),
                upi_reference=row["upi_reference"])
            record = Record(record_id=row["payment_id"], order_id=row["order_id"],
                            payment=payment, created_at=row["captured_at"])
            record.settlement_lines.append(SettlementLine(
                entity_id=row["payment_id"], settlement_id=settlement_id,
                type="payment", payment_id=row["payment_id"],
                order_id=row["order_id"], amount=row["amount"],
                fee=row["fee"], tax=row["tax"], utr=utr, settled_at=settled_at))

            if row["refunded"]:
                refund_id = _rzp_id(self._rng, "rfnd_")
                record.refund = Refund(refund_id, row["payment_id"],
                                       row["amount"], row["captured_at"])
                # No fee reversal on the refund line. Rule 8.
                record.settlement_lines.append(SettlementLine(
                    entity_id=refund_id, settlement_id=settlement_id,
                    type="refund", payment_id=row["payment_id"],
                    order_id=row["order_id"], amount=-row["amount"],
                    fee=0, tax=0, utr=utr, settled_at=settled_at))

            records.append(record)

        credited = sum(ln.amount - ln.fee - ln.tax
                       for r in records for ln in r.settlement_lines)
        batch = Batch(records=records,
                      bank_credits=[BankCredit(utr, credited, settled_at)],
                      seed=0,                    # not generated; nothing to seed
                      rate_card=rate_card)
        return batch

    def commit_settlement(self, batch: Batch, model: str = "", effort: str = "",
                          via_batch: bool = False) -> str:
        """Persist the batch as a run and mark its payments settled."""
        run_id = self.store.save_run(batch, model=model, effort=effort,
                                     via_batch=via_batch)
        self.conn.execute(
            "INSERT INTO business_runs (run_id, business_id, created_at)"
            " VALUES (?,?,?)", (run_id, self._scoped(), int(time.time())))
        for record in batch.records:
            self.conn.execute(
                "UPDATE live_payments SET settled_run_id = ? WHERE payment_id = ?",
                (run_id, record.record_id))
        self.conn.commit()
        return run_id

    def settlements(self) -> list:
        return self.conn.execute(
            "SELECT r.*, ("
            "  SELECT COUNT(*) FROM variances v WHERE v.run_id = r.run_id"
            " ) AS findings FROM runs r JOIN business_runs br"
            " ON br.run_id = r.run_id WHERE br.business_id = ?"
            " ORDER BY r.created_at DESC", (self._scoped(),)).fetchall()

    def owns_run(self, run_id: str) -> bool:
        """One business must not be able to open another's settlement by id."""
        return self.conn.execute(
            "SELECT 1 FROM business_runs WHERE run_id = ? AND business_id = ?",
            (run_id, self._scoped())).fetchone() is not None

    def load_batch(self, run_id: str, rate_card: dict) -> Optional[Batch]:
        """
        Rebuild a stored settlement so it can be re-audited without re-entering it.

        A run is immutable data; re-auditing it after a rule changes is a normal
        thing to want, and it must produce the same inputs every time.
        """
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE run_id = ?", (run_id,)).fetchall()
        if not rows:
            return None
        lines = self.conn.execute(
            "SELECT * FROM settlement_lines WHERE run_id = ?", (run_id,)).fetchall()
        by_payment: dict[str, list] = {}
        for line in lines:
            by_payment.setdefault(line["payment_id"], []).append(line)

        records = []
        for row in rows:
            payment = Payment(
                payment_id=row["payment_id"], amount=row["amount"],
                method=row["method"], card_network=row["card_network"],
                card_type=row["card_type"],
                is_international=bool(row["is_international"]),
                upi_reference=row["upi_reference"])
            record = Record(record_id=row["payment_id"], order_id=row["order_id"],
                            payment=payment, created_at=row["created_at"])
            for line in by_payment.get(row["payment_id"], []):
                record.settlement_lines.append(SettlementLine(
                    entity_id=line["entity_id"], settlement_id=line["settlement_id"],
                    type=line["type"], payment_id=line["payment_id"],
                    order_id=line["order_id"], amount=line["amount"],
                    fee=line["fee"], tax=line["tax"], utr=line["utr"],
                    settled_at=line["settled_at"]))
                if line["type"] == "refund":
                    record.refund = Refund(line["entity_id"], line["payment_id"],
                                           -line["amount"], line["settled_at"])
            records.append(record)

        credits = [BankCredit(r["utr"], r["amount"], r["credited_at"])
                   for r in self.conn.execute(
                       "SELECT * FROM bank_credits WHERE run_id = ?", (run_id,))]
        return Batch(records=records, bank_credits=credits, seed=0,
                     rate_card=rate_card)
