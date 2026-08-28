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

-- What the cash forecaster is built from: balances, scheduled outflows, and
-- the monthly charges nobody schedules.
--
-- One table with a `kind`, for the same reason recon_sources is one table:
-- they share a lifecycle - replaced together, scoped together, forgotten
-- together - and three tables always written in the same breath is three
-- chances for one to be left behind.
CREATE TABLE IF NOT EXISTS treasury_inputs (
  business_id  TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- 'account' | 'payout' | 'recurring'
  ref          TEXT NOT NULL,
  payload      TEXT NOT NULL,      -- the record as JSON
  source_file  TEXT,
  uploaded_at  INTEGER,
  PRIMARY KEY (business_id, kind, ref)
);

-- The three sources of the three-way reconciliation, as a merchant uploaded
-- them. Stored rather than held for one run: assembling three exports is a
-- real piece of work, and asking for all three again because somebody
-- refreshed the page is how a tool stops being used.
--
-- `kind` is 'invoice' | 'settlement' | 'bank'. One table rather than three
-- because they share a lifecycle - replaced together, scoped together,
-- forgotten together - and three tables that are always written in the same
-- breath is three chances for one of them to be left behind.
CREATE TABLE IF NOT EXISTS recon_sources (
  business_id  TEXT NOT NULL,
  kind         TEXT NOT NULL,
  ref          TEXT NOT NULL,      -- invoice_id | txn_id | utr_number
  payload      TEXT NOT NULL,      -- the record as JSON
  source_file  TEXT,
  uploaded_at  INTEGER,
  PRIMARY KEY (business_id, kind, ref)
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
  -- Whether anybody KNOWS what happened to the GSTR-3B, as against it being
  -- known not to have been filed. Without this column the round trip through
  -- storage collapsed the two: a GSTR-2B history, which can rarely see
  -- payment, came back out of the database with every period reading "did not
  -- pay" - and every supplier in a merchant's book was branded a defaulter.
  -- Defaults to 1 so rows written before this column existed, which came from
  -- CSVs that DO carry payment dates, keep their meaning.
  gstr3b_known    INTEGER DEFAULT 1,
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
        from merchant.businesses import _add_column

        _add_column(self.store.conn, "supplier_filing_history",
                    "gstr3b_known", "INTEGER DEFAULT 1")
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

    def _supplier_behaviour(self, business_id: str, supplier_name: str):
        """
        How THIS supplier files, given what the simulator is set to.

        With one behaviour selected everybody gets it, which is what the
        setting always did. With several, each supplier is assigned one - and
        the assignment has to be sticky, because filing behaviour is a property
        of the supplier rather than of an invoice. A supplier who misfiles to a
        Karnataka registration does it every time, which is precisely why a
        cross-GSTIN search finds them; re-rolling per invoice would leave no
        consistent wrong registration to search for and quietly break the
        finding.

        So a supplier who already has one keeps it, and only genuinely new ones
        draw from the rotation. The lookup is against what was actually stored
        on their past invoices, so the assignment survives a restart without
        needing a table of its own.
        """
        from merchant.suppliers import (SupplierBehaviour, next_behaviour,
                                        parse_behaviours)

        chosen = self.businesses.supplier_behaviour(business_id)
        options = parse_behaviours(chosen)
        if len(options) == 1:
            return options[0]

        allowed = {str(o) for o in options}
        seen = self.conn.execute(
            "SELECT behaviour FROM live_purchases WHERE business_id = ?"
            " AND supplier_name = ? AND behaviour IS NOT NULL"
            " ORDER BY recorded_at LIMIT 1",
            (business_id, supplier_name)).fetchone()
        # Reassign only if their old behaviour is no longer among the selected
        # ones - which means the merchant deliberately turned it off, and the
        # page already promises that new invoices follow the new setting.
        if seen is not None and seen["behaviour"] in allowed:
            return SupplierBehaviour(seen["behaviour"])

        assigned = self.conn.execute(
            "SELECT COUNT(DISTINCT supplier_name) n FROM live_purchases"
            " WHERE business_id = ? AND behaviour IN"
            f" ({','.join('?' * len(allowed))})",
            (business_id, *sorted(allowed))).fetchone()["n"]
        return next_behaviour(chosen, assigned)

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
        behaviour = (SupplierBehaviour(behaviour) if behaviour
                     else self._supplier_behaviour(business_id, supplier_name))
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

    def _simulated_behaviour(self, business_id: str, group):
        """
        How this supplier files, for a demo register.

        Rotates through whichever behaviours the simulator has switched on,
        keyed on the supplier's position in the register so the spread is even
        and the same register always tells the same story. Deterministic on
        purpose - a demo whose findings changed on every click would be worse
        than one with a single finding.
        """
        from merchant.suppliers import next_behaviour, parse_behaviours

        chosen = self.businesses.supplier_behaviour(business_id)
        options = parse_behaviours(chosen)
        if len(options) == 1:
            return options[0]
        seat = getattr(self, "_demo_seat", 0)
        self._demo_seat = seat + 1
        return next_behaviour(chosen, seat)

    def replace_purchase_register(self, imported, simulate_filing: bool = False
                                  ) -> dict:
        """
        Store an uploaded register, replacing whatever was held before.

        Replacing rather than appending: a register is a statement of a
        period's purchases, and a merchant who uploads a corrected export
        expects it to correct things rather than double them. Anything already
        reconciled keeps its run id so the findings still point at real rows.

        ## simulate_filing, and why it is off by default

        A purchase register is only half the reconciliation. The other half is
        GSTR-2B - what the suppliers reported about the same invoices - and
        the gap between them is the entire finding.

        For a real upload that half must come from the merchant's own GSTR-2B,
        and manufacturing it here would be inventing the evidence the product
        exists to check against. So this defaults to off.

        For the DEMO it has to be on, and its absence was a real defect: the
        demo stored purchases and no GSTR-2B at all, so every invoice
        reconciled as absent_from_2b and the four discrepancies the engine can
        actually tell apart - a wrong GSTIN, a short-reported tax, a late
        filing, a clean match - never appeared. A demo that can only produce
        one finding demonstrates almost nothing.
        """
        import time

        from merchant.suppliers import current_period, file_invoice

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

        if simulate_filing:
            self.conn.execute(
                "DELETE FROM live_gstr2b WHERE business_id = ?", (business_id,))
            filed_rows = []
            for group in imported.groups:
                # One behaviour per supplier, from whichever ones the simulator
                # has switched on. Per supplier rather than per invoice for the
                # same reason record_purchase does it: a supplier who misfiles
                # to another state does it every time, and that consistency is
                # what makes a cross-GSTIN search find them.
                behaviour = self._simulated_behaviour(business_id, group)
                for invoice in group.invoices:
                    when = _as_date(invoice.invoice_date)
                    filed = file_invoice(
                        supplier_gstin=group.supplier_gstin,
                        invoice_number=invoice.invoice_number,
                        invoice_date=when,
                        taxable_value=invoice.taxable_value,
                        cgst=invoice.cgst, sgst=invoice.sgst, igst=invoice.igst,
                        period=current_period(when), behaviour=behaviour)
                    if filed is None:
                        continue
                    filed_rows.append((
                        f"2b_{secrets.token_hex(6)}", business_id,
                        filed["supplier_gstin"], filed["invoice_number"],
                        str(filed["invoice_date"]), filed["taxable_value"],
                        filed["cgst"], filed["sgst"], filed["igst"],
                        filed["filed_period"], now))
            self.conn.executemany(
                "INSERT INTO live_gstr2b (entry_id, business_id,"
                " supplier_gstin, invoice_number, invoice_date, taxable_value,"
                " cgst, sgst, igst, filed_period, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", filed_rows)

        self.conn.executemany(
            "INSERT INTO live_purchases (purchase_id, business_id,"
            " supplier_name, supplier_gstin, invoice_number, invoice_date,"
            " taxable_value, cgst, sgst, igst, category, paid_on, behaviour,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return {"added": len(rows), "removed": removed}

    # --- the cash forecaster's inputs -------------------------------------

    def replace_treasury_input(self, kind: str, records,
                               filename: str = "") -> int:
        """Store balances, payouts or recurring charges, replacing the last lot."""
        import json
        import time

        business_id = self._scoped()
        self.conn.execute(
            "DELETE FROM treasury_inputs WHERE business_id = ? AND kind = ?",
            (business_id, kind))

        now = int(time.time())
        rows = []
        for index, record in enumerate(records):
            payload = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in vars(record).items()}
            ref = str(payload.get("account_id") or payload.get("payout_id")
                      or payload.get("name") or index)
            rows.append((business_id, kind, ref, json.dumps(payload),
                         filename, now))

        self.conn.executemany(
            "INSERT OR REPLACE INTO treasury_inputs (business_id, kind, ref,"
            " payload, source_file, uploaded_at) VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def treasury_inputs(self, *, receipts=None):
        """
        The stored inputs, in the shape the forecaster already takes.

        Returns None without balances and payouts. A forecast needs a starting
        point and something to spend - without either it is not a cautious
        forecast, it is a straight line, and drawing one would be worse than
        saying what is missing.

        Recurring charges are OPTIONAL and inferred from payout history when
        absent, because a curve that silently omits rent is cheerful and wrong
        in the direction that hurts.
        """
        import json
        from datetime import date as _date

        from engine.treasury.records import (BankAccount, RecurringExpense,
                                             ScheduledPayout, TreasuryInputs)
        from merchant.treasury_import import infer_recurring

        held: dict = {"account": [], "payout": [], "recurring": []}
        for row in self.conn.execute(
                "SELECT kind, payload FROM treasury_inputs"
                " WHERE business_id = ? ORDER BY ref", (self._scoped(),)):
            held.setdefault(row["kind"], []).append(json.loads(row["payload"]))

        if not held["account"] or not held["payout"]:
            return None

        def when(value):
            try:
                return _date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                return _date.today()

        out = TreasuryInputs(as_of=_date.today())
        out.accounts = [BankAccount(
            account_id=r["account_id"], nickname=r["nickname"],
            balance=r["balance"], as_of=when(r["as_of"]),
            overdraft_limit=r.get("overdraft_limit", 0))
            for r in held["account"]]
        out.payouts = [ScheduledPayout(
            payout_id=r["payout_id"], payee=r["payee"], amount=r["amount"],
            due_on=when(r["due_on"]), kind=r.get("kind", "vendor"))
            for r in held["payout"]]

        if held["recurring"]:
            out.recurring = [RecurringExpense(
                name=r["name"], amount=r["amount"],
                day_of_month=r["day_of_month"], kind=r.get("kind", "recurring"),
                seen_in_months=r.get("seen_in_months", 0),
                confidence=r.get("confidence", 1.0))
                for r in held["recurring"]]
        else:
            out.recurring = infer_recurring(out.payouts)

        out.receipts = list(receipts or [])
        return out

    def treasury_held(self) -> dict:
        """What is on file for each input, for the page to describe it."""
        out = {}
        for row in self.conn.execute(
                "SELECT kind, COUNT(*) n, MAX(uploaded_at) at,"
                " MAX(source_file) f FROM treasury_inputs"
                " WHERE business_id = ? GROUP BY kind", (self._scoped(),)):
            out[row["kind"]] = {"records": row["n"], "uploaded_at": row["at"],
                                "source_file": row["f"] or ""}
        return out

    def forget_treasury_inputs(self) -> int:
        removed = self.conn.execute(
            "DELETE FROM treasury_inputs WHERE business_id = ?",
            (self._scoped(),)).rowcount
        self.conn.commit()
        return removed

    # --- the three-way sources --------------------------------------------

    def replace_recon_source(self, kind: str, records, filename: str = ""
                             ) -> int:
        """
        Store one of the three sources, replacing what was held before.

        Replacing rather than merging, for the same reason the purchase
        register does: an export is a statement of a period, and a merchant
        who uploads a corrected one expects it to correct rather than double.
        """
        import json
        import time

        business_id = self._scoped()
        self.conn.execute(
            "DELETE FROM recon_sources WHERE business_id = ? AND kind = ?",
            (business_id, kind))

        now = int(time.time())
        rows = []
        for record in records:
            payload = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in vars(record).items()}
            ref = str(payload.get("invoice_id") or payload.get("txn_id")
                      or payload.get("utr_number") or "")
            if not ref:
                continue
            rows.append((business_id, kind, ref, json.dumps(payload),
                         filename, now))

        self.conn.executemany(
            "INSERT OR REPLACE INTO recon_sources (business_id, kind, ref,"
            " payload, source_file, uploaded_at) VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def recon_batch(self):
        """
        The three stored sources, in the shape the matcher already takes.

        Returns None when any of the three is missing. A two-way join is a
        different product with different findings, and quietly running one
        while calling it a three-way reconciliation would be the dishonest
        option - so the caller is told which source is absent instead.
        """
        import json
        from datetime import date as _date

        from engine.recon.records import (BankCredit, Invoice, ReconBatch,
                                          Settlement)

        held: dict[str, list] = {"invoice": [], "settlement": [], "bank": []}
        for row in self.conn.execute(
                "SELECT kind, payload FROM recon_sources WHERE business_id = ?"
                " ORDER BY ref", (self._scoped(),)):
            held.setdefault(row["kind"], []).append(json.loads(row["payload"]))

        if not all(held.get(k) for k in ("invoice", "settlement", "bank")):
            return None

        def when(value):
            try:
                return _date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                return _date.today()

        return ReconBatch(
            invoices=[Invoice(
                invoice_id=r["invoice_id"], customer_name=r["customer_name"],
                amount=r["amount"], date_issued=when(r["date_issued"]),
                status=r.get("status", "issued")) for r in held["invoice"]],
            settlements=[Settlement(
                txn_id=r["txn_id"], gross_amount=r["gross_amount"],
                fee_deducted=r["fee_deducted"], net_settled=r["net_settled"],
                settlement_date=when(r["settlement_date"]),
                invoice_reference=r.get("invoice_reference"),
                utr=r.get("utr")) for r in held["settlement"]],
            bank=[BankCredit(
                utr_number=r["utr_number"], description=r["description"],
                credit_amount=r["credit_amount"],
                transaction_date=when(r["transaction_date"]))
                for r in held["bank"]])

    def recon_sources_held(self) -> dict:
        """What is on file for each source, for the page to describe it."""
        out = {}
        for row in self.conn.execute(
                "SELECT kind, COUNT(*) n, MAX(uploaded_at) at,"
                " MAX(source_file) f FROM recon_sources"
                " WHERE business_id = ? GROUP BY kind", (self._scoped(),)):
            out[row["kind"]] = {"records": row["n"], "uploaded_at": row["at"],
                                "source_file": row["f"] or ""}
        return out

    def forget_recon_sources(self) -> int:
        removed = self.conn.execute(
            "DELETE FROM recon_sources WHERE business_id = ?",
            (self._scoped(),)).rowcount
        self.conn.commit()
        return removed

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
                    int(month.gstr3b_known),
                    history.registration_status, imported.filename, now))

        self.conn.executemany(
            "INSERT INTO supplier_filing_history (business_id, supplier_gstin,"
            " period, gstr1_filed, gstr3b_filed, gstr3b_known,"
            " registration_status, source_file, uploaded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
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
                "gstr3b_filed": row["gstr3b_filed"],
                "gstr3b_known": bool(row["gstr3b_known"])})
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
                        is_international: bool = False,
                        captured_at: Optional[int] = None) -> str:
        """
        Take the money. The gateway decides its own fee; we record what it did.

        Note what is NOT happening here: nothing checks whether the fee is
        correct. That is the auditor's job and it happens after settlement,
        which is exactly the problem the product exists for - the merchant
        cannot see the error at the moment it is made.

        `captured_at` exists for tests, and it is not a convenience.

        Settlement is T+2 working days, and the detector raises
        PERIOD_BOUNDARY when a sale and its settlement fall in different
        months - correctly, because that is a real thing a merchant has to
        reclassify. Tests that captured at "now" therefore passed for most of
        a month and went red in its last few days, with no code change: on
        28 August 2026 a same-day sale settles on 1 September.

        A test asserting "this is CLEAN" has to control the date, or it is
        asserting something about the calendar rather than about the auditor.
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
             str(self.behaviour()),
             int(captured_at if captured_at is not None else time.time())))
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
        # T+2 from the LAST payment in the batch, not from now.
        #
        # A settlement follows the payments it settles; it does not follow the
        # moment somebody pressed the button. In the simulator those are
        # minutes apart so it never showed - until a test pinned its capture
        # dates and the settlement still landed two working days from today,
        # in a different month, raising a PERIOD_BOUNDARY the test had no way
        # to control.
        #
        # It also made the whole suite calendar-dependent: green for most of a
        # month, red in the last few days, flipping mid-afternoon on 28 August
        # 2026 with no code change.
        latest = max((row["captured_at"] or 0) for row in pending)
        settled_at = int(add_working_days(
            datetime.fromtimestamp(latest, timezone.utc),
            SETTLEMENT_WORKING_DAYS).timestamp())

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


def _as_date(text):
    """An invoice date from a register row, as a date."""
    from datetime import date as _date

    try:
        return _date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return _date.today()
