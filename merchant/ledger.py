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

CREATE TABLE IF NOT EXISTS business_recon_runs (
  run_id      TEXT PRIMARY KEY,
  business_id TEXT NOT NULL,
  source      TEXT,
  n_records   INTEGER,
  created_at  INTEGER
);

-- What the three-way join could not resolve, one row per exception. Matched
-- lines are not stored here - the only question anything downstream ever
-- asks is "did the reconciler flag this payment", never "was it ever clean".
--
-- This did not exist until the cash forecaster needed to ask the
-- reconciler whether a receipt it is counting on was ever flagged.
-- Settlement and GST findings both survive a restart already; recon's
-- lived only in the run-state dict, which is fine for a page a person is
-- looking at and not enough for another agent to check later.
CREATE TABLE IF NOT EXISTS recon_findings (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT,
  business_id  TEXT NOT NULL,
  invoice_id   TEXT,
  txn_id       TEXT,
  utr_number   TEXT,
  finding      TEXT,
  variance     INTEGER,
  at_stake     INTEGER,
  action       TEXT,
  reasoning    TEXT,
  detail       TEXT,
  created_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_recon_findings_run ON recon_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_recon_findings_txn
  ON recon_findings(business_id, txn_id);

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

-- --- TDS credit tracking --------------------------------------------------
--
-- Two more sources that share nothing with settlement or ITC but the
-- business they belong to: what Razorpay's own settlement report says it
-- withheld, and what the merchant's own government tax-credit statement
-- (Form 26AS before 1 April 2026, Form 168 after) shows as credited.
--
-- The join key is payment_id - unlike ITC's cross-entity GSTIN+invoice-
-- number match, both sides here trace to the same Razorpay payment on the
-- same merchant's own PAN. A REAL Form 26AS/168 carries no such reference
-- (see engine/tds/generator.py's docstring); keeping payment_id is a
-- deliberate simplification for an exact, measurable demo.

CREATE TABLE IF NOT EXISTS live_tds_deductions (
  deduction_id    TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  payment_id      TEXT,
  gross_amount    INTEGER,
  section_code    TEXT,
  rate_bps        INTEGER,
  amount          INTEGER,
  deducted_at     TEXT,
  recorded_at     INTEGER,
  reconciled_run  TEXT
);

CREATE TABLE IF NOT EXISTS live_tds_credits (
  credit_id       TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  payment_id      TEXT,
  form            TEXT,
  code_shown      TEXT,
  amount          INTEGER,
  credited_period TEXT,
  posted_at       TEXT,
  recorded_at     INTEGER
);

CREATE TABLE IF NOT EXISTS business_tds_runs (
  run_id       TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL,
  period       TEXT,
  n_deductions INTEGER,
  created_at   INTEGER
);

CREATE TABLE IF NOT EXISTS tds_findings (
  id                 INTEGER PRIMARY KEY,
  run_id             TEXT,
  business_id        TEXT NOT NULL,
  payment_id         TEXT,
  deducted_at        TEXT,
  deducted_amount    INTEGER,
  deducted_rate_bps  INTEGER,
  deducted_code      TEXT,
  credited_amount    INTEGER,
  credited_code      TEXT,
  credited_form      TEXT,
  credited_period    TEXT,
  expected_rate_bps  INTEGER,
  expected_code      TEXT,
  expected_form      TEXT,
  delta              INTEGER,
  tolerance          INTEGER,
  exception_code     TEXT,
  action             TEXT,
  confidence         REAL,
  reasoning          TEXT,
  rule_cited         TEXT,
  decided_by         TEXT,
  money_at_stake     INTEGER,
  queued_for_human   INTEGER,
  evidence           TEXT,          -- JSON: the signals, for "show the working"
  created_at         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tds_deductions_biz ON live_tds_deductions(business_id);
CREATE INDEX IF NOT EXISTS idx_tds_credits_biz    ON live_tds_credits(business_id);
CREATE INDEX IF NOT EXISTS idx_tds_findings_run    ON tds_findings(run_id);

-- --- payout timing ---------------------------------------------------------
--
-- One verdict per RUN here, not one per record - there is only ever one
-- pattern to judge (see engine/payout_timing/detector.py). The per-record
-- rows in payout_timing_findings exist so the results page can show the
-- distribution and the worst offenders, not because any of them was judged
-- separately.

CREATE TABLE IF NOT EXISTS business_payout_timing_runs (
  run_id            TEXT PRIMARY KEY,
  business_id       TEXT NOT NULL,
  n_settled         INTEGER,
  n_on_time         INTEGER,
  n_sla_miss        INTEGER,
  n_unmatched       INTEGER,
  miss_rate_bps     INTEGER,
  mean_delay_days   REAL,
  max_delay_days    INTEGER,
  total_float_cost  INTEGER,
  pattern           TEXT,
  action            TEXT,
  confidence        REAL,
  reasoning         TEXT,
  escalation_text   TEXT,
  decided_by        TEXT,
  queued_for_human  INTEGER,
  errored           INTEGER DEFAULT 0,
  source            TEXT,
  created_at        INTEGER
);

CREATE TABLE IF NOT EXISTS payout_timing_findings (
  id                    INTEGER PRIMARY KEY,
  run_id                TEXT,
  business_id           TEXT NOT NULL,
  invoice_id            TEXT,
  txn_id                TEXT,
  invoice_amount        INTEGER,
  net_settled           INTEGER,
  due_date              TEXT,
  settlement_date       TEXT,
  delay_working_days    INTEGER,
  delay_calendar_days   INTEGER,
  float_cost_paise      INTEGER,
  code                  TEXT,
  created_at            INTEGER
);

CREATE INDEX IF NOT EXISTS idx_payout_timing_findings_run
  ON payout_timing_findings(run_id);

-- --- GST output tax (gst_filing) --------------------------------------
--
-- The outward side of GST, previously uncovered - gst_itc is entirely about
-- purchases. Four source/config tables plus one run/findings family per
-- layer, mirroring the itc_findings/business_itc_runs split.

CREATE TABLE IF NOT EXISTS live_sale_invoices (
  invoice_id      TEXT,
  business_id     TEXT NOT NULL,
  invoice_number  TEXT,
  invoice_date    TEXT,
  buyer_name      TEXT,
  buyer_gstin     TEXT,            -- '' / NULL = unregistered buyer
  place_of_supply TEXT,            -- 2-digit state code
  hsn_code        TEXT,
  taxable_value   INTEGER,         -- paise
  cgst            INTEGER,
  sgst            INTEGER,
  igst            INTEGER,
  invoice_type    TEXT,            -- 'b2b' | 'b2cl' | 'b2cs' - set by classify()
                                    -- even for an unconfigured-HSN invoice, so
                                    -- the UI can say what it WOULD have been;
                                    -- `code` below is the field that actually
                                    -- says whether it belongs in a GSTR-1 table
  code            TEXT,            -- GSTR1Code: CLASSIFIED | IRN_MISSING |
                                    -- HSN_RATE_UNCONFIGURED
  irn             TEXT,
  irn_required    INTEGER,
  period          TEXT,            -- 'YYYY-MM'
  recorded_at     INTEGER,
  filed_run       TEXT,
  PRIMARY KEY (business_id, invoice_id)
);

-- GST rates ARE HSN-linked in reality; no official free lookup exists
-- anywhere in this codebase. Merchant-entered config, same design as
-- business_rate_card (the merchant's own MDR contract, not a shared file).
CREATE TABLE IF NOT EXISTS business_hsn_rate_card (
  business_id  TEXT,
  hsn_code     TEXT,
  description  TEXT,
  rate_bps     INTEGER,
  PRIMARY KEY (business_id, hsn_code)
);

-- Layer 2's input. gstr1a window state is DERIVED from gstr3b_filed at
-- compute time (locked once filed, open until then), not stored redundantly.
CREATE TABLE IF NOT EXISTS gst_filing_cycles (
  business_id                TEXT NOT NULL,
  period                     TEXT NOT NULL,
  gstr1_filed                TEXT,
  gstr1_liability            INTEGER,
  gstr3b_filed                TEXT,          -- NULL = window still open
  gstr3b_paid                INTEGER,
  wrongly_claimed_itc_paise  INTEGER DEFAULT 0,
  qrmp_opted                 INTEGER DEFAULT 0,
  PRIMARY KEY (business_id, period)
);

-- Layer 3's input. No real API exists for a merchant's own live ledger
-- balance (the true balance depends on the portal's whole history of past
-- utilisation and reversals this system never sees) - merchant-entered,
-- mirroring business_rate_card; demo variant plants a synthetic snapshot.
CREATE TABLE IF NOT EXISTS live_gst_ledger_balances (
  business_id  TEXT NOT NULL,
  as_of        TEXT,
  credit_igst  INTEGER,
  credit_cgst  INTEGER,
  credit_sgst  INTEGER,
  cash_igst    INTEGER,
  cash_cgst    INTEGER,
  cash_sgst    INTEGER,
  source       TEXT,               -- 'demo' | 'merchant_entered'
  recorded_at  INTEGER,
  PRIMARY KEY (business_id, as_of)
);

CREATE TABLE IF NOT EXISTS business_gstr1_runs (
  run_id          TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  period          TEXT,
  n_invoices      INTEGER,
  n_b2b           INTEGER,
  n_b2cl          INTEGER,
  n_b2cs          INTEGER,
  n_missing_irn   INTEGER,
  n_unconfigured  INTEGER,
  total_taxable   INTEGER,
  total_tax       INTEGER,
  created_at      INTEGER
);

CREATE TABLE IF NOT EXISTS gst_correction_findings (        -- layer 2, per period
  id                  INTEGER PRIMARY KEY,
  run_id              TEXT,
  business_id         TEXT NOT NULL,
  period              TEXT,
  gstr1_liability     INTEGER,
  gstr3b_paid         INTEGER,
  delta               INTEGER,
  tolerance           INTEGER,
  window_state        TEXT,
  exception_code      TEXT,
  action              TEXT,
  confidence          REAL,
  reasoning           TEXT,
  rule_cited          TEXT,
  interest_paise      INTEGER,
  interest_rate_bps   INTEGER,
  days_overdue        INTEGER,
  decided_by          TEXT,
  money_at_stake      INTEGER,
  queued_for_human    INTEGER,
  gstr1a_draft        TEXT,
  drc03_draft         TEXT,
  created_at          INTEGER
);

CREATE TABLE IF NOT EXISTS gst_offset_findings (             -- layer 3, per period
  id                    INTEGER PRIMARY KEY,
  run_id                TEXT,
  business_id           TEXT NOT NULL,
  period                TEXT,
  liability_igst        INTEGER,
  liability_cgst        INTEGER,
  liability_sgst        INTEGER,
  credit_igst           INTEGER,
  credit_cgst           INTEGER,
  credit_sgst           INTEGER,
  offset_igst_to_igst   INTEGER,
  offset_igst_to_cgst   INTEGER,
  offset_igst_to_sgst   INTEGER,
  offset_cgst_to_cgst   INTEGER,
  offset_sgst_to_sgst   INTEGER,
  cash_igst_needed      INTEGER,
  cash_cgst_needed      INTEGER,
  cash_sgst_needed      INTEGER,
  rule_88c_breach       INTEGER,
  breach_amount         INTEGER,
  exception_code        TEXT,
  reasoning             TEXT,
  rule_cited            TEXT,
  pmt06_draft           TEXT,
  drc01b_draft          TEXT,
  created_at            INTEGER
);

CREATE TABLE IF NOT EXISTS gst_qrmp_findings (                -- layer 4, per quarter
  id                  INTEGER PRIMARY KEY,
  run_id              TEXT,
  business_id         TEXT NOT NULL,
  quarter             TEXT,
  turnover_paise      INTEGER,
  eligible            INTEGER,
  method              TEXT,
  fixed_sum_paise     INTEGER,
  self_assessed_paise INTEGER,
  month1_pmt06        INTEGER,
  month2_pmt06        INTEGER,
  iff_used_month1     INTEGER,
  iff_used_month2     INTEGER,
  reasoning           TEXT,
  quarterly_gstr3b    TEXT,           -- JSON: month-3 aggregation, see
                                       -- engine.gst_filing.qrmp.build_quarterly_gstr3b
  created_at          INTEGER
);

-- One scalar per business, the same shape as businesses.review_above_paise
-- (merchant/businesses.py) but kept in its own table rather than added as a
-- column to that shared, already-heavily-depended-on table - lower risk,
-- same reasoning every other GST filing config table this checkpoint set
-- used its own table instead of widening an existing one.
CREATE TABLE IF NOT EXISTS business_qrmp_settings (
  business_id            TEXT PRIMARY KEY,
  iff_materiality_paise  INTEGER NOT NULL
);

-- What the real GSTR-1/e-invoice exports need and this system never had
-- anywhere else: the business's own GSTIN and registered address. Merchant-
-- entered, same shape as business_hsn_rate_card - no real API for a
-- business's own registration details exists anywhere in this codebase.
CREATE TABLE IF NOT EXISTS business_gst_profile (
  business_id     TEXT PRIMARY KEY,
  gstin           TEXT,
  legal_name      TEXT,
  trade_name      TEXT,
  address_line1   TEXT,
  location        TEXT,
  pincode         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sale_invoices_biz ON live_sale_invoices(business_id);
CREATE INDEX IF NOT EXISTS idx_gstr1_runs_biz ON business_gstr1_runs(business_id);
CREATE INDEX IF NOT EXISTS idx_gst_correction_findings_run ON gst_correction_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_gst_offset_findings_run ON gst_offset_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_gst_qrmp_findings_run ON gst_qrmp_findings(run_id);

-- --- the vendor invoice auditor -----------------------------------------
--
-- The purchase-side commercial fields live_purchases never carried, because
-- ITC reconciliation only ever needed the GST columns. A child table rather
-- than widening live_purchases: a purchase imported for ITC alone (no line
-- items known) stays a valid row with nothing here referencing it, and a
-- CSV/Zoho pull that DOES carry line items adds rows here without touching
-- the ITC path at all.
CREATE TABLE IF NOT EXISTS live_purchase_line_items (
  line_item_id      TEXT PRIMARY KEY,
  purchase_id       TEXT NOT NULL,
  business_id       TEXT NOT NULL,
  supplier_name     TEXT,
  supplier_gstin    TEXT,
  invoice_number    TEXT,
  invoice_date      TEXT,
  description       TEXT,
  item_key          TEXT,             -- normalise_item_key() of description;
                                       -- the rate-card join key
  quantity_x100     INTEGER,          -- integer hundredths, not a float
  unit_price_paise  INTEGER,
  line_total_paise  INTEGER,
  recorded_at       INTEGER,
  reconciled_run    TEXT
);

-- The merchant's own negotiated price per item per supplier. No real API
-- for this exists anywhere (the same honest gap business_hsn_rate_card and
-- business_gst_profile document above) - merchant-entered, one row per
-- (supplier, item) pair.
CREATE TABLE IF NOT EXISTS business_vendor_rate_card (
  business_id                   TEXT,
  supplier_gstin                TEXT,
  item_key                      TEXT,
  description                   TEXT,
  contracted_unit_price_paise   INTEGER,
  source                        TEXT,
  PRIMARY KEY (business_id, supplier_gstin, item_key)
);

CREATE TABLE IF NOT EXISTS business_vendor_terms_runs (
  run_id       TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL,
  n_items      INTEGER,
  source       TEXT DEFAULT 'demo',
  created_at   INTEGER
);

CREATE TABLE IF NOT EXISTS vendor_terms_findings (
  id                            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id                        TEXT,
  business_id                   TEXT NOT NULL,
  line_item_id                  TEXT,
  purchase_id                   TEXT,
  supplier_name                 TEXT,
  supplier_gstin                TEXT,
  invoice_number                TEXT,
  invoice_date                  TEXT,
  description                   TEXT,
  quantity_x100                 INTEGER,
  unit_price_paise              INTEGER,
  contracted_unit_price_paise   INTEGER,
  money_at_stake_paise          INTEGER,
  code                          TEXT,
  action                        TEXT,
  confidence                    REAL,
  reasoning                     TEXT,
  decided_by                    TEXT,
  queued_for_human              INTEGER,
  credit_note_text              TEXT,
  created_at                    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_purchase_line_items_biz
  ON live_purchase_line_items(business_id);
CREATE INDEX IF NOT EXISTS idx_vendor_terms_findings_run
  ON vendor_terms_findings(run_id);

-- --- the chargeback defence assembler -----------------------------------
--
-- The dispute notice itself is real and pullable (Razorpay's own
-- GET /v1/disputes) - the evidence behind it is not, anywhere. Evidence
-- stays a separate child table, text only in v1 (no Documents API wiring
-- yet - see agent/chargeback_documents.py's own docstring for why a file
-- blob would be an unscoped abstraction right now).
CREATE TABLE IF NOT EXISTS live_disputes (
  dispute_id         TEXT PRIMARY KEY,   -- disp_... from Razorpay, or a local id
  business_id        TEXT NOT NULL,
  payment_id         TEXT,
  amount_paise       INTEGER,
  reason_code        TEXT,
  reason_description TEXT,
  phase              TEXT,
  status             TEXT,
  respond_by         INTEGER,            -- unix ts, the real deadline
  source             TEXT,               -- 'demo' | 'manual' | 'razorpay'
  recorded_at        INTEGER,
  reconciled_run     TEXT
);

-- What the merchant actually has, one row per (dispute, evidence type).
-- evidence_type matches the real Contest API's own field names exactly -
-- see engine/chargeback/rules.py's own docstring on why.
CREATE TABLE IF NOT EXISTS dispute_evidence_items (
  dispute_id     TEXT NOT NULL,
  business_id    TEXT NOT NULL,
  evidence_type  TEXT NOT NULL,
  detail         TEXT,
  recorded_at    INTEGER,
  PRIMARY KEY (business_id, dispute_id, evidence_type)
);

CREATE TABLE IF NOT EXISTS business_chargeback_runs (
  run_id       TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL,
  n_disputes   INTEGER,
  source       TEXT DEFAULT 'demo',
  created_at   INTEGER
);

CREATE TABLE IF NOT EXISTS chargeback_findings (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              TEXT,
  business_id         TEXT NOT NULL,
  dispute_id          TEXT,
  reason_code         TEXT,
  amount_paise        INTEGER,
  respond_by          INTEGER,
  code                TEXT,
  action              TEXT,
  confidence          REAL,
  reasoning           TEXT,
  decided_by          TEXT,
  queued_for_human    INTEGER,
  evidence_pack_json  TEXT,       -- {"summary": "...", "explanation_letter": "..."}
  created_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_live_disputes_biz ON live_disputes(business_id);
CREATE INDEX IF NOT EXISTS idx_dispute_evidence_biz
  ON dispute_evidence_items(business_id, dispute_id);
CREATE INDEX IF NOT EXISTS idx_chargeback_findings_run
  ON chargeback_findings(run_id);
"""





# The instrument shapes the simulator offers, mirrored from app.INSTRUMENTS
# so the batch generator does not import the web layer.
INSTRUMENT_SHAPES = {
    "upi": ("upi", None, None, False),
    "rupay_debit": ("card", "rupay", "debit", False),
    "visa_debit": ("card", "visa", "debit", False),
    "visa_credit": ("card", "visa", "credit", False),
    "amex": ("card", "amex", "credit", False),
    "international": ("card", "visa", "credit", True),
    "netbanking": ("netbanking", None, None, False),
    "wallet": ("wallet", None, None, False),
}


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
        # Where a settlement run came from. Existing rows default to
        # 'simulator', which is what every run predating the Razorpay
        # importer actually was.
        _add_column(self.store.conn, "runs", "source",
                    "TEXT DEFAULT 'simulator'")
        # Resolution memory predates business scoping - CLAUDE.md section 12
        # was written when this was a single-business tool. Without this
        # column one merchant's confirmed resolution could be recalled for
        # another merchant's variance with the same exception code, which is
        # exactly the leak this platform's scoping exists to prevent.
        _add_column(self.store.conn, "resolution_memory",
                    "business_id", "TEXT DEFAULT ''")
        # gst_qrmp_findings predates the month-3 quarterly aggregation -
        # same trap as above, caught a third time now.
        _add_column(self.store.conn, "gst_qrmp_findings",
                    "quarterly_gstr3b", "TEXT")
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
        """
        One behaviour, for the callers that take a single payment.

        The setting can now hold several - see businesses.behaviours - so the
        stored value may be a list. This returns the first, which keeps every
        single-payment path working unchanged; the batch generator asks for
        the whole set instead.
        """
        from merchant.gateway import parse_behaviours

        return parse_behaviours(self.businesses.behaviour(self._scoped()))[0]

    def behaviours(self) -> list:
        """Every fault currently switched on."""
        return self.businesses.behaviours(self._scoped())

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

    # --- TDS credit tracking -----------------------------------------------

    def seed_tds_demo(self, n: int = 60, seed: Optional[int] = None) -> int:
        """
        Generate a demo deduction history and credit statement, in one call.

        Demo Mode is the only way this agent gets data in v1 - see
        engine/tds/generator.py's docstring on why a real Form 26AS/168
        cannot be joined by payment_id, which is what a future Upload tab
        will have to solve. Every behaviour the generator can plant is
        planted here every time, same as the other agents' demo seeding.
        """
        import time

        from engine.tds.generator import generate_batch

        business_id = self._scoped()
        kwargs = {"n": n}
        if seed is not None:
            kwargs["seed"] = seed
        batch, _truth = generate_batch(**kwargs)
        now = int(time.time())

        self.conn.executemany(
            "INSERT INTO live_tds_deductions (deduction_id, business_id,"
            " payment_id, gross_amount, section_code, rate_bps, amount,"
            " deducted_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(f"tds_{secrets.token_hex(6)}", business_id, d.payment_id,
              d.gross_amount, d.section_code, d.rate_bps, d.amount,
              str(d.deducted_at), now) for d in batch.deductions])
        self.conn.executemany(
            "INSERT INTO live_tds_credits (credit_id, business_id,"
            " payment_id, form, code_shown, amount, credited_period,"
            " posted_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(f"cr_{secrets.token_hex(6)}", business_id, c.payment_id,
              c.form, c.code_shown, c.amount, c.credited_period,
              str(c.posted_at), now) for c in batch.credits])
        self.conn.commit()
        return len(batch.deductions)

    def unreconciled_tds_deductions(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_tds_deductions WHERE business_id = ?"
            " AND reconciled_run IS NULL ORDER BY recorded_at",
            (self._scoped(),)).fetchall()

    def build_tds_batch(self, only_unreconciled: bool = True):
        """
        Assemble the merchant's real deductions and credit lines into the
        shape engine/tds already takes, mirroring build_itc_batch.
        """
        from engine.tds.generator import CreditEntry, Deduction, TdsBatch

        rows = (self.unreconciled_tds_deductions() if only_unreconciled
               else self.conn.execute(
                   "SELECT * FROM live_tds_deductions WHERE business_id = ?"
                   " ORDER BY recorded_at", (self._scoped(),)).fetchall())
        if not rows:
            return None

        deductions = [Deduction(
            payment_id=r["payment_id"], gross_amount=r["gross_amount"],
            section_code=r["section_code"], rate_bps=r["rate_bps"],
            amount=r["amount"], deducted_at=date.fromisoformat(r["deducted_at"])
        ) for r in rows]

        credit_rows = self.conn.execute(
            "SELECT * FROM live_tds_credits WHERE business_id = ?",
            (self._scoped(),)).fetchall()
        credits = [CreditEntry(
            payment_id=c["payment_id"], form=c["form"],
            code_shown=c["code_shown"], amount=c["amount"],
            credited_period=c["credited_period"],
            posted_at=date.fromisoformat(c["posted_at"])
        ) for c in credit_rows]

        return TdsBatch(deductions=deductions, credits=credits,
                        as_of=date.today())

    def commit_tds_run(self, batch, period: str = "") -> str:
        import time

        from merchant.suppliers import current_period

        run_id = f"tds_run_{secrets.token_hex(6)}"
        business_id = self._scoped()
        self.conn.execute(
            "INSERT INTO business_tds_runs (run_id, business_id, period,"
            " n_deductions, created_at) VALUES (?,?,?,?,?)",
            (run_id, business_id, period or current_period(),
             len(batch.deductions), int(time.time())))
        self.conn.executemany(
            "UPDATE live_tds_deductions SET reconciled_run = ?"
            " WHERE payment_id = ? AND business_id = ?",
            [(run_id, d.payment_id, business_id) for d in batch.deductions])
        self.conn.commit()
        return run_id

    def record_tds_findings(self, run_id: str, variances, decisions,
                            verdicts=None) -> None:
        import json
        import time

        by_id = {v.payment_id: v for v in variances}
        verdict_by_id = {v.payment_id: v for v in (verdicts or [])}
        now = int(time.time())
        rows = []

        for decision in decisions:
            variance = by_id.get(decision.payment_id)
            if variance is None:
                continue
            verdict = verdict_by_id.get(decision.payment_id)
            evidence = json.dumps([{
                "kind": s.kind, "detail": s.detail, "rule": s.rule,
                "source": s.source, "amount_paise": s.amount_paise,
            } for s in variance.signals])
            rows.append((
                run_id, self._scoped(), variance.payment_id,
                str(variance.deducted_at), variance.deducted_amount,
                variance.deducted_rate_bps, variance.deducted_code,
                variance.credited_amount, variance.credited_code,
                variance.credited_form, variance.credited_period,
                variance.expected_rate_bps, variance.expected_code,
                variance.expected_form, variance.delta, variance.tolerance,
                decision.exception_code, decision.action, decision.confidence,
                (verdict.reasoning if verdict else variance.reasoning) or "",
                (verdict.rule_cited if verdict else variance.rule_cited) or "",
                decision.decided_by, decision.money_at_stake,
                int(decision.queued_for_human), evidence, now))

        self.conn.executemany(
            "INSERT INTO tds_findings (run_id, business_id, payment_id,"
            " deducted_at, deducted_amount, deducted_rate_bps, deducted_code,"
            " credited_amount, credited_code, credited_form, credited_period,"
            " expected_rate_bps, expected_code, expected_form, delta,"
            " tolerance, exception_code, action, confidence, reasoning,"
            " rule_cited, decided_by, money_at_stake, queued_for_human,"
            " evidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.commit()

    def tds_findings(self, run_id: str, needing_action: bool = False) -> list:
        from engine.tds.taxonomy import NO_ACTION

        sql = "SELECT * FROM tds_findings WHERE business_id = ? AND run_id = ?"
        params = [self._scoped(), run_id]
        if needing_action:
            quiet = ",".join("?" * len(NO_ACTION))
            sql += f" AND exception_code NOT IN ({quiet})"
            params += [str(c) for c in NO_ACTION]
        return self.conn.execute(
            sql + " ORDER BY money_at_stake DESC", params).fetchall()

    def latest_tds_run(self):
        return self.conn.execute(
            "SELECT * FROM business_tds_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    # --- payout timing -------------------------------------------------

    def commit_payout_timing_run(self, summary, decision, verdict=None,
                                 source: str = "demo") -> str:
        """
        One row for the whole run - the verdict IS the record here, not an
        aggregate over per-record decisions, since there is only ever one
        pattern judged per run. `verdict` carries the reasoning/escalation
        text when the agent ran; `decision` alone (calculator-only) falls
        back to the arithmetic's own explanation.
        """
        import time

        run_id = f"payout_{secrets.token_hex(6)}"
        business_id = self._scoped()
        self.conn.execute(
            "INSERT INTO business_payout_timing_runs (run_id, business_id,"
            " n_settled, n_on_time, n_sla_miss, n_unmatched, miss_rate_bps,"
            " mean_delay_days, max_delay_days, total_float_cost, pattern,"
            " action, confidence, reasoning, escalation_text, decided_by,"
            " queued_for_human, errored, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, business_id, summary.n_settled, summary.n_on_time,
             summary.n_sla_miss, summary.n_unmatched, summary.miss_rate_bps,
             summary.mean_delay_working_days, summary.max_delay_working_days,
             decision.float_cost_paise, decision.pattern, decision.action,
             decision.confidence,
             verdict.reasoning if verdict else summary.detail,
             verdict.escalation_text if verdict else None,
             decision.decided_by, int(decision.queued_for_human),
             int(decision.errored), source, int(time.time())))
        self.conn.commit()
        return run_id

    def record_payout_timing_findings(self, run_id: str, summary) -> None:
        import time

        business_id = self._scoped()
        now = int(time.time())
        rows = [(
            run_id, business_id, r.invoice_id, r.txn_id, r.invoice_amount,
            r.net_settled, str(r.due_date),
            str(r.settlement_date) if r.settlement_date else None,
            r.delay_working_days, r.delay_calendar_days, r.float_cost_paise,
            r.code, now) for r in summary.records]
        self.conn.executemany(
            "INSERT INTO payout_timing_findings (run_id, business_id,"
            " invoice_id, txn_id, invoice_amount, net_settled, due_date,"
            " settlement_date, delay_working_days, delay_calendar_days,"
            " float_cost_paise, code, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def payout_timing_findings(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM payout_timing_findings WHERE business_id = ?"
            " AND run_id = ? ORDER BY delay_working_days DESC",
            (self._scoped(), run_id)).fetchall()

    def latest_payout_timing_run(self):
        return self.conn.execute(
            "SELECT * FROM business_payout_timing_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    # --- GST filing: layer 1, outward sales / GSTR-1 --------------------

    def set_hsn_rate(self, hsn_code: str, rate_bps: int,
                     description: str = "") -> None:
        self.conn.execute(
            "INSERT INTO business_hsn_rate_card (business_id, hsn_code,"
            " description, rate_bps) VALUES (?,?,?,?)"
            " ON CONFLICT (business_id, hsn_code) DO UPDATE SET"
            " description = excluded.description, rate_bps = excluded.rate_bps",
            (self._scoped(), hsn_code.strip(), description, rate_bps))
        self.conn.commit()

    def hsn_rate_card(self) -> dict:
        """hsn_code -> rate_bps, the shape engine.gst_filing.classifier wants."""
        rows = self.conn.execute(
            "SELECT * FROM business_hsn_rate_card WHERE business_id = ?",
            (self._scoped(),)).fetchall()
        return {r["hsn_code"]: r["rate_bps"] for r in rows}

    def hsn_rate_rows(self) -> list:
        return self.conn.execute(
            "SELECT * FROM business_hsn_rate_card WHERE business_id = ?"
            " ORDER BY hsn_code", (self._scoped(),)).fetchall()

    def seed_gst_filing_demo(self, n: int = 40, seed: Optional[int] = None
                             ) -> tuple[int, dict]:
        """Generate a demo outward-sales batch and its rate card, in one
        call. Returns (n, ground_truth) - ground_truth maps invoice_id to
        the GSTR1Code the generator built it to produce, for
        engine.gst_filing.scoring to check the classifier against."""
        import time

        from engine.gst_filing.generator import DEMO_RATE_CARD, generate_invoices

        business_id = self._scoped()
        kwargs = {"n": n}
        if seed is not None:
            kwargs["seed"] = seed
        invoices, truth = generate_invoices(**kwargs)
        now = int(time.time())

        for hsn_code, rate_bps in DEMO_RATE_CARD.items():
            self.set_hsn_rate(hsn_code, rate_bps, description="demo")

        # OR REPLACE, not a plain INSERT: the demo batch is built from a
        # fixed default seed, so running Demo Mode a second time for the
        # same business regenerates the same invoice_ids - a fresh,
        # unfiled row each time is what "run it again" should mean, not a
        # UNIQUE-constraint crash.
        self.conn.executemany(
            "INSERT OR REPLACE INTO live_sale_invoices (invoice_id,"
            " business_id, invoice_number, invoice_date, buyer_name,"
            " buyer_gstin, place_of_supply, hsn_code, taxable_value, irn,"
            " period, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(inv.invoice_id, business_id, inv.invoice_number,
              str(inv.invoice_date), inv.buyer_name, inv.buyer_gstin or "",
              inv.place_of_supply, inv.hsn_code, inv.taxable_value,
              inv.irn or "", str(inv.invoice_date)[:7], now)
             for inv in invoices])
        self.conn.commit()
        return len(invoices), truth

    def import_razorpay_invoices(self, raw_items: list) -> dict:
        """
        Real outward invoices, pulled from Razorpay - alongside
        seed_gst_filing_demo(), never instead of it. Does NOT touch the HSN
        rate card the way the demo seeder does: a real business's rate card
        is its own configured contract, not something to overwrite with
        demo rates. Returns {"imported": n, "skipped": [(id, reason), ...]}
        - see engine.gst_filing.razorpay_import for what gets skipped and
        why.
        """
        import time

        from engine.gst_filing.razorpay_import import from_razorpay_batch

        invoices, skipped = from_razorpay_batch(raw_items)
        business_id = self._scoped()
        now = int(time.time())

        # OR REPLACE: re-syncing the same invoice from Razorpay (an edit, or
        # just running sync again) should refresh the row, not collide with
        # it - same reasoning as the demo seeder's own OR REPLACE.
        self.conn.executemany(
            "INSERT OR REPLACE INTO live_sale_invoices (invoice_id,"
            " business_id, invoice_number, invoice_date, buyer_name,"
            " buyer_gstin, place_of_supply, hsn_code, taxable_value, irn,"
            " period, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(inv.invoice_id, business_id, inv.invoice_number,
              str(inv.invoice_date), inv.buyer_name, inv.buyer_gstin or "",
              inv.place_of_supply, inv.hsn_code, inv.taxable_value,
              inv.irn or "", str(inv.invoice_date)[:7], now)
             for inv in invoices])
        self.conn.commit()
        return {"imported": len(invoices), "skipped": skipped}

    def unfiled_razorpay_invoice_count(self) -> int:
        """Real, unfiled invoices pulled from Razorpay - never counts a
        Demo Mode row. The two never collide on invoice_id (Razorpay's own
        ids start "inv_"; the demo generator's start "INV-"), so the split
        is exact, not a guess."""
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM live_sale_invoices WHERE business_id = ?"
            " AND filed_run IS NULL AND invoice_id LIKE 'inv\\_%' ESCAPE '\\'",
            (self._scoped(),)).fetchone()
        return row["n"] if row else 0

    def unfiled_sale_invoices(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_sale_invoices WHERE business_id = ?"
            " AND filed_run IS NULL ORDER BY invoice_date",
            (self._scoped(),)).fetchall()

    def build_gstr1_batch(self, only_unfiled: bool = True) -> Optional[list]:
        """Assemble the merchant's real outward invoices into the shape
        engine/gst_filing/classifier.py already takes."""
        from engine.gst_filing.classifier import OutwardInvoice

        rows = (self.unfiled_sale_invoices() if only_unfiled else
               self.conn.execute(
                   "SELECT * FROM live_sale_invoices WHERE business_id = ?"
                   " ORDER BY invoice_date", (self._scoped(),)).fetchall())
        if not rows:
            return None
        return [OutwardInvoice(
            invoice_id=r["invoice_id"], invoice_number=r["invoice_number"],
            invoice_date=date.fromisoformat(r["invoice_date"]),
            buyer_name=r["buyer_name"], buyer_gstin=r["buyer_gstin"] or None,
            place_of_supply=r["place_of_supply"], hsn_code=r["hsn_code"],
            taxable_value=r["taxable_value"], irn=r["irn"] or None)
            for r in rows]

    def commit_gstr1_run(self, classified: list, draft, period: str = "") -> str:
        import time

        run_id = f"gstr1_{secrets.token_hex(6)}"
        business_id = self._scoped()
        # Counted from the draft's own tables, not the raw classified list -
        # an unconfigured-HSN invoice still gets an invoice_type assigned by
        # classify() (so the UI can say what it WOULD have been), but
        # assemble_gstr1() correctly excludes it from every table, and the
        # run's own headline counts have to agree with what the draft shows,
        # not double-count something the draft itself left out.
        n_b2b, n_b2cl, n_b2cs = len(draft.b2b), len(draft.b2cl), len(draft.b2cs)

        self.conn.execute(
            "INSERT INTO business_gstr1_runs (run_id, business_id, period,"
            " n_invoices, n_b2b, n_b2cl, n_b2cs, n_missing_irn,"
            " n_unconfigured, total_taxable, total_tax, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, business_id, period, len(classified), n_b2b, n_b2cl,
             n_b2cs, len(draft.missing_irn), len(draft.unconfigured),
             draft.total_taxable, draft.total_tax, int(time.time())))

        self.conn.executemany(
            "UPDATE live_sale_invoices SET cgst = ?, sgst = ?, igst = ?,"
            " invoice_type = ?, code = ?, irn_required = ?, filed_run = ?"
            " WHERE invoice_id = ? AND business_id = ?",
            [(c.cgst, c.sgst, c.igst, c.invoice_type, str(c.code),
              int(c.code == "IRN_MISSING" or (c.irn is not None)),
              run_id, c.invoice_id, business_id) for c in classified])
        self.conn.commit()
        return run_id

    def latest_gstr1_run(self):
        return self.conn.execute(
            "SELECT * FROM business_gstr1_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    def sale_invoices_in_run(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM live_sale_invoices WHERE business_id = ?"
            " AND filed_run = ? ORDER BY invoice_type, invoice_date",
            (self._scoped(), run_id)).fetchall()

    # --- GST filing: layer 2, GSTR-1A / DRC-03 correction timing ---------

    def upsert_filing_cycle(self, cycle) -> None:
        """`cycle` is an engine.gst_filing.timing.FilingCycle."""
        self.conn.execute(
            "INSERT INTO gst_filing_cycles (business_id, period, gstr1_filed,"
            " gstr1_liability, gstr3b_filed, gstr3b_paid,"
            " wrongly_claimed_itc_paise) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT (business_id, period) DO UPDATE SET"
            " gstr1_liability = excluded.gstr1_liability,"
            " gstr3b_filed = excluded.gstr3b_filed,"
            " gstr3b_paid = excluded.gstr3b_paid,"
            " wrongly_claimed_itc_paise = excluded.wrongly_claimed_itc_paise",
            (self._scoped(), cycle.period, str(cycle.period),
             cycle.gstr1_liability,
             str(cycle.gstr3b_filed) if cycle.gstr3b_filed else None,
             cycle.gstr3b_paid, cycle.wrongly_claimed_itc_paise))
        self.conn.commit()

    def filing_cycles(self) -> list:
        """Every planted/recorded period for this business, as
        engine.gst_filing.timing.FilingCycle objects, oldest first."""
        from engine.gst_filing.timing import FilingCycle

        rows = self.conn.execute(
            "SELECT * FROM gst_filing_cycles WHERE business_id = ?"
            " ORDER BY period", (self._scoped(),)).fetchall()
        return [FilingCycle(
            period=r["period"], gstr1_liability=r["gstr1_liability"] or 0,
            gstr3b_filed=(date.fromisoformat(r["gstr3b_filed"])
                         if r["gstr3b_filed"] else None),
            gstr3b_paid=r["gstr3b_paid"] or 0,
            wrongly_claimed_itc_paise=r["wrongly_claimed_itc_paise"] or 0)
            for r in rows]

    def seed_gst_correction_demo(self, current_period: str,
                                 current_liability_paise: int
                                 ) -> tuple[int, dict]:
        """Plants four prior periods (clean / locked-normal /
        locked-wrong-itc / Rule-88C-breach) alongside the current one, so
        layers 2 and 3 have something to judge the first time an agent runs
        the pipeline. Returns (n, ground_truth) - ground_truth maps period
        to the CorrectionCode it was built to produce."""
        from engine.gst_filing.generator import generate_cycles

        cycles, truth = generate_cycles(current_period, current_liability_paise)
        for c in cycles:
            self.upsert_filing_cycle(c)
        return len(cycles), truth

    def record_correction_findings(self, run_id: str, findings,
                                   decisions: dict) -> None:
        """`findings` are CorrectionFinding objects, `decisions` maps
        period -> engine.gst_filing.gate.CorrectionDecision."""
        import json
        import time

        from engine.gst_filing.timing import drc03_draft, gstr1a_draft

        now = int(time.time())
        business_id = self._scoped()
        rows = []
        for f in findings:
            d = decisions.get(f.period)
            reasoning = (d.priority_reasoning if d and d.priority_reasoning
                        else f.reasoning)
            g1a = (json.dumps(gstr1a_draft(f))
                  if f.action == "file_1a" else None)
            drc03 = (json.dumps(drc03_draft(f))
                    if f.action == "pay_drc03" else None)
            rows.append((
                run_id, business_id, f.period, f.gstr1_liability,
                f.gstr3b_paid, f.delta, f.tolerance, f.window_state,
                f.exception_code, f.action,
                d.confidence if d else 1.0, reasoning, f.rule_cited,
                f.interest_paise, f.interest_rate_bps, f.days_overdue,
                d.decided_by if d else "calculator",
                (d.money_at_stake if d else abs(f.delta) + f.interest_paise),
                int(d.queued_for_human) if d else 0, g1a, drc03, now))
        self.conn.executemany(
            "INSERT INTO gst_correction_findings (run_id, business_id,"
            " period, gstr1_liability, gstr3b_paid, delta, tolerance,"
            " window_state, exception_code, action, confidence, reasoning,"
            " rule_cited, interest_paise, interest_rate_bps, days_overdue,"
            " decided_by, money_at_stake, queued_for_human, gstr1a_draft,"
            " drc03_draft, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def correction_findings_for_run(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM gst_correction_findings WHERE business_id = ?"
            " AND run_id = ? ORDER BY period", (self._scoped(), run_id)
            ).fetchall()

    # --- GST filing: layer 3, ITC offset hierarchy / Rule 88C shield -----

    def set_gst_ledger_balance(self, as_of: str, *, credit_igst: int,
                               credit_cgst: int, credit_sgst: int,
                               cash_igst: int, cash_cgst: int,
                               cash_sgst: int, source: str) -> None:
        import time

        self.conn.execute(
            "INSERT INTO live_gst_ledger_balances (business_id, as_of,"
            " credit_igst, credit_cgst, credit_sgst, cash_igst, cash_cgst,"
            " cash_sgst, source, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (business_id, as_of) DO UPDATE SET"
            " credit_igst = excluded.credit_igst,"
            " credit_cgst = excluded.credit_cgst,"
            " credit_sgst = excluded.credit_sgst,"
            " cash_igst = excluded.cash_igst,"
            " cash_cgst = excluded.cash_cgst,"
            " cash_sgst = excluded.cash_sgst,"
            " source = excluded.source",
            (self._scoped(), as_of, credit_igst, credit_cgst, credit_sgst,
             cash_igst, cash_cgst, cash_sgst, source, int(time.time())))
        self.conn.commit()

    def latest_gst_ledger_balance(self):
        return self.conn.execute(
            "SELECT * FROM live_gst_ledger_balances WHERE business_id = ?"
            " ORDER BY as_of DESC, recorded_at DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    def seed_gst_offset_demo(self, liability, as_of: str) -> None:
        """
        Plants a credit/cash snapshot sized off THIS period's own liability
        (by head), not fixed rupee amounts - so the scenario holds together
        regardless of exactly which invoices the fixed-seed generator
        produced. Deliberately covers all of IGST's own liability plus
        spills into CGST, with zero direct CGST credit, so a naive
        per-head-only allocator would ask for the full CGST liability in
        cash while the real hierarchy does not - see
        engine/gst_filing/offset.py's module docstring.
        """
        credit_igst = liability.igst + (liability.cgst * 60 // 100)
        credit_sgst = liability.sgst * 40 // 100
        cash_cgst = liability.cgst // 20            # a little already on hand
        self.set_gst_ledger_balance(
            as_of, credit_igst=credit_igst, credit_cgst=0,
            credit_sgst=credit_sgst, cash_igst=0, cash_cgst=cash_cgst,
            cash_sgst=0, source="demo")

    def record_offset_findings(self, run_id: str, findings,
                               drc01b_bodies: Optional[dict] = None) -> None:
        """`findings` are engine.gst_filing.offset.OffsetFinding objects.
        `drc01b_bodies` maps period -> the drafted document's body text,
        for breach periods the caller already ran through
        agent/gst_filing_documents.py::drc01b_response()."""
        import json
        import time

        from engine.gst_filing.offset import pmt06_draft

        drc01b_bodies = drc01b_bodies or {}
        now = int(time.time())
        business_id = self._scoped()
        rows = []
        for f in findings:
            p = f.plan
            pmt06 = json.dumps(pmt06_draft(f)) if p is not None else None
            drc01b = drc01b_bodies.get(f.period)
            rows.append((
                run_id, business_id, f.period,
                p.liability.igst if p else 0, p.liability.cgst if p else 0,
                p.liability.sgst if p else 0,
                p.credit.igst if p else 0, p.credit.cgst if p else 0,
                p.credit.sgst if p else 0,
                p.offset_igst_to_igst if p else 0,
                p.offset_igst_to_cgst if p else 0,
                p.offset_igst_to_sgst if p else 0,
                p.offset_cgst_to_cgst if p else 0,
                p.offset_sgst_to_sgst if p else 0,
                p.cash_igst_needed if p else 0,
                p.cash_cgst_needed if p else 0,
                p.cash_sgst_needed if p else 0,
                int(f.rule_88c_breach), f.breach_amount, f.exception_code,
                f.reasoning, f.rule_cited, pmt06, drc01b, now))
        self.conn.executemany(
            "INSERT INTO gst_offset_findings (run_id, business_id, period,"
            " liability_igst, liability_cgst, liability_sgst, credit_igst,"
            " credit_cgst, credit_sgst, offset_igst_to_igst,"
            " offset_igst_to_cgst, offset_igst_to_sgst, offset_cgst_to_cgst,"
            " offset_sgst_to_sgst, cash_igst_needed, cash_cgst_needed,"
            " cash_sgst_needed, rule_88c_breach, breach_amount,"
            " exception_code, reasoning, rule_cited, pmt06_draft,"
            " drc01b_draft, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.commit()

    def offset_findings_for_run(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM gst_offset_findings WHERE business_id = ?"
            " AND run_id = ? ORDER BY period", (self._scoped(), run_id)
            ).fetchall()

    # --- GST filing: layer 4, QRMP method choice / IFF plan --------------

    DEFAULT_IFF_MATERIALITY_PAISE = 2_000_00      # Rs 2,000 - a demo default,
                                                   # not a statutory figure;
                                                   # IFF has no legal per-
                                                   # invoice value cap to cite

    def iff_materiality(self) -> int:
        row = self.conn.execute(
            "SELECT iff_materiality_paise FROM business_qrmp_settings"
            " WHERE business_id = ?", (self._scoped(),)).fetchone()
        return row["iff_materiality_paise"] if row else self.DEFAULT_IFF_MATERIALITY_PAISE

    def set_iff_materiality(self, paise: int) -> None:
        self.conn.execute(
            "INSERT INTO business_qrmp_settings (business_id,"
            " iff_materiality_paise) VALUES (?,?)"
            " ON CONFLICT (business_id) DO UPDATE SET"
            " iff_materiality_paise = excluded.iff_materiality_paise",
            (self._scoped(), paise))
        self.conn.commit()

    def set_gst_profile(self, *, gstin: str = "", legal_name: str = "",
                        trade_name: str = "", address_line1: str = "",
                        location: str = "", pincode: str = "") -> None:
        self.conn.execute(
            "INSERT INTO business_gst_profile (business_id, gstin,"
            " legal_name, trade_name, address_line1, location, pincode)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT (business_id) DO UPDATE SET"
            " gstin = excluded.gstin, legal_name = excluded.legal_name,"
            " trade_name = excluded.trade_name,"
            " address_line1 = excluded.address_line1,"
            " location = excluded.location, pincode = excluded.pincode",
            (self._scoped(), gstin, legal_name, trade_name, address_line1,
             location, pincode))
        self.conn.commit()

    def gst_profile(self) -> dict:
        row = self.conn.execute(
            "SELECT * FROM business_gst_profile WHERE business_id = ?",
            (self._scoped(),)).fetchone()
        if not row:
            return {"gstin": "", "legal_name": "", "trade_name": "",
                   "address_line1": "", "location": "", "pincode": ""}
        return {k: (row[k] or "") for k in
               ("gstin", "legal_name", "trade_name", "address_line1",
                "location", "pincode")}

    def record_qrmp_finding(self, run_id: str, finding,
                            quarterly_gstr3b: Optional[dict] = None) -> None:
        """`finding` is an engine.gst_filing.qrmp.QRMPFinding.
        `quarterly_gstr3b` is build_quarterly_gstr3b()'s own dict, only
        present when the quarter was eligible."""
        import json
        import time

        self.conn.execute(
            "INSERT INTO gst_qrmp_findings (run_id, business_id, quarter,"
            " turnover_paise, eligible, method, fixed_sum_paise,"
            " self_assessed_paise, month1_pmt06, month2_pmt06,"
            " iff_used_month1, iff_used_month2, reasoning, quarterly_gstr3b,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, self._scoped(), finding.quarter, finding.turnover_paise,
             int(finding.is_eligible), finding.method, finding.fixed_sum_paise,
             finding.self_assessed_paise, finding.month1_pmt06,
             finding.month2_pmt06, finding.iff_used_month1,
             finding.iff_used_month2, finding.reasoning,
             json.dumps(quarterly_gstr3b) if quarterly_gstr3b else None,
             int(time.time())))
        self.conn.commit()

    def qrmp_finding_for_run(self, run_id: str):
        return self.conn.execute(
            "SELECT * FROM gst_qrmp_findings WHERE business_id = ?"
            " AND run_id = ? ORDER BY created_at DESC LIMIT 1",
            (self._scoped(), run_id)).fetchone()

    def commit_recon_run(self, source: str, n_records: int) -> str:
        """
        Register one reconciliation run, so its findings have somewhere to
        attach to. Called whether or not there is anything to store - a
        clean run still needs a run_id on record, the same way a settlement
        with zero findings still gets a row in `runs`.
        """
        import time

        run_id = f"recon_{secrets.token_hex(6)}"
        self.conn.execute(
            "INSERT INTO business_recon_runs (run_id, business_id, source,"
            " n_records, created_at) VALUES (?,?,?,?,?)",
            (run_id, self._scoped(), source, n_records, int(time.time())))
        self.conn.commit()
        return run_id

    def record_recon_findings(self, run_id: str, rows) -> None:
        """
        Store the exceptions one three-way run left over - not the matched
        lines, which nothing downstream ever needs to ask about again.

        `rows` are ReconRow objects; only the unresolved ones are written.
        """
        import time

        now = int(time.time())
        business_id = self._scoped()
        to_write = [
            (run_id, business_id,
             row.invoice.invoice_id if row.invoice else None,
             row.settlement.txn_id if row.settlement else None,
             row.bank.utr_number if row.bank else None,
             row.finding, row.variance, row.at_stake, row.action,
             row.reasoning, row.detail, now)
            for row in rows if not row.resolved]
        if not to_write:
            return
        self.conn.executemany(
            "INSERT INTO recon_findings (run_id, business_id, invoice_id,"
            " txn_id, utr_number, finding, variance, at_stake, action,"
            " reasoning, detail, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", to_write)
        self.conn.commit()

    def latest_recon_run(self):
        return self.conn.execute(
            "SELECT * FROM business_recon_runs WHERE business_id = ?"
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

    def recon_batch(self, requires=("invoice", "settlement", "bank")):
        """
        The stored sources, in the shape the matcher already takes.

        Returns None when any REQUIRED source is missing. A two-way join is a
        different product with different findings, and quietly running one
        while calling it a three-way reconciliation would be the dishonest
        option - so the caller is told which source is absent instead.

        `requires` exists because not every agent reading these sources needs
        all three. The payout timing detector passes `bank=[]` itself (see
        engine/payout_timing/detector.py) - it compares when a sale was made
        against when the gateway settled it, and a bank statement has no part
        in that question. Demanding one would refuse to run on data that
        answers the question completely.
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

        if not all(held.get(k) for k in requires):
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

    # What payout timing needs on file, and nothing more. Named here rather
    # than spelled out at each call site so the two entry points - an uploaded
    # settlement report and a Razorpay pull - cannot drift apart on what they
    # consider enough to run.
    PAYOUT_TIMING_SOURCES = ("invoice", "settlement")

    def settled_payout_batch(self):
        """
        Payout timing over the settlements THIS platform produced.

        Razorpay never settles in test mode, so the recon report a merchant
        could point this agent at is permanently empty there. But the
        settlements made here carry both dates the question needs - when the
        payment was captured, and when the batch settled - so the loop can be
        closed without leaving the platform at all.

        Returns None when nothing has been settled yet. Every other source
        this agent reads returns None rather than an empty answer for the
        same reason: "no delay found" and "nothing to look at" are different
        statements and must not share a screen.
        """
        from datetime import date as _date
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from engine.recon.records import Invoice, ReconBatch, Settlement

        rows = self.conn.execute(
            "SELECT p.payment_id, p.amount, p.created_at, p.method,"
            "       sl.fee, sl.tax, sl.settled_at"
            " FROM payments p"
            " JOIN business_runs br ON br.run_id = p.run_id"
            " JOIN settlement_lines sl ON sl.run_id = p.run_id"
            "   AND sl.payment_id = p.payment_id AND sl.type = 'payment'"
            " WHERE br.business_id = ? AND sl.settled_at > 0",
            (self._scoped(),)).fetchall()
        if not rows:
            return None

        def when(ts):
            return _dt.fromtimestamp(ts, _tz.utc).date() if ts else _date.today()

        invoices, settlements = [], []
        for r in rows:
            fee = (r["fee"] or 0) + (r["tax"] or 0)
            invoices.append(Invoice(
                invoice_id=r["payment_id"],
                customer_name=(r["method"] or "sale").upper(),
                amount=r["amount"], date_issued=when(r["created_at"]),
                status="issued"))
            settlements.append(Settlement(
                txn_id=r["payment_id"], gross_amount=r["amount"],
                fee_deducted=fee, net_settled=r["amount"] - fee,
                settlement_date=when(r["settled_at"]),
                invoice_reference=r["payment_id"], utr=None))
        return ReconBatch(invoices=invoices, settlements=settlements, bank=[])

    def payout_timing_batch(self):
        """
        Real sales and real settlements, ready for the payout timing detector.

        Deliberately does not require a bank statement: this agent asks when
        the gateway settled a sale against when it promised to, which the
        invoice and settlement dates answer between them. See recon_batch's
        `requires`.
        """
        return self.recon_batch(requires=self.PAYOUT_TIMING_SOURCES)

    def payout_timing_missing(self) -> list:
        """Which of the sources this agent needs are not on file yet."""
        held = self.recon_sources_held()
        return [k for k in self.PAYOUT_TIMING_SOURCES if not held.get(k)]

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

    # --- vendor invoice auditor --------------------------------------------

    def set_vendor_rate(self, supplier_gstin: str, description: str,
                        contracted_unit_price_paise: int, source: str = ""
                        ) -> None:
        from engine.vendor_terms.rules import normalise_item_key

        self.conn.execute(
            "INSERT INTO business_vendor_rate_card (business_id,"
            " supplier_gstin, item_key, description,"
            " contracted_unit_price_paise, source) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (business_id, supplier_gstin, item_key) DO UPDATE"
            " SET description = excluded.description,"
            " contracted_unit_price_paise = excluded.contracted_unit_price_paise,"
            " source = excluded.source",
            (self._scoped(), supplier_gstin.strip().upper(),
             normalise_item_key(description), description.strip(),
             contracted_unit_price_paise, source))
        self.conn.commit()

    def vendor_rate_card(self) -> dict:
        """(supplier_gstin, item_key) -> contracted price, the shape
        engine.vendor_terms.detector wants."""
        rows = self.conn.execute(
            "SELECT * FROM business_vendor_rate_card WHERE business_id = ?",
            (self._scoped(),)).fetchall()
        return {(r["supplier_gstin"], r["item_key"]):
               r["contracted_unit_price_paise"] for r in rows}

    def vendor_rate_rows(self) -> list:
        return self.conn.execute(
            "SELECT * FROM business_vendor_rate_card WHERE business_id = ?"
            " ORDER BY supplier_gstin, description", (self._scoped(),)).fetchall()

    def seed_vendor_terms_demo(self, n: int = 40, seed: Optional[int] = None
                               ) -> tuple[int, dict]:
        """Plant a demo line-item batch and its matching rate card, in one
        call - same trick every other demo seeder in this codebase uses."""
        import time

        from engine.vendor_terms.generator import generate_line_items

        business_id = self._scoped()
        kwargs = {"n": n}
        if seed is not None:
            kwargs["seed"] = seed
        items, truth, rate_card = generate_line_items(**kwargs)
        now = int(time.time())

        # OR REPLACE, not a plain INSERT: the demo batch is built from a
        # fixed default seed, so running Demo Mode a second time for the
        # same business regenerates the same line_item_ids - a fresh,
        # unreconciled row each time is what "run it again" should mean,
        # not a UNIQUE-constraint crash. Same fix seed_gst_filing_demo
        # already applies for the identical reason.
        self.conn.executemany(
            "INSERT OR REPLACE INTO live_purchase_line_items (line_item_id,"
            " purchase_id, business_id, supplier_name, supplier_gstin,"
            " invoice_number, invoice_date, description, item_key,"
            " quantity_x100, unit_price_paise, line_total_paise, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i.line_item_id, i.purchase_id, business_id, i.supplier_name,
              i.supplier_gstin, i.invoice_number, str(i.invoice_date),
              i.description, i.item_key, i.quantity_x100, i.unit_price_paise,
              i.line_total_paise, now) for i in items])
        # A human-readable description per item_key, read off the billed
        # items themselves rather than the normalised key - "cement opc 53
        # grade bag" is a valid join key but not something a merchant wants
        # to read on their own rate card.
        descriptions = {i.item_key: i.description for i in items}
        self.conn.executemany(
            "INSERT INTO business_vendor_rate_card (business_id,"
            " supplier_gstin, item_key, description,"
            " contracted_unit_price_paise, source) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (business_id, supplier_gstin, item_key) DO NOTHING",
            [(business_id, gstin, item_key,
              descriptions.get(item_key, item_key), price, "demo contract")
             for (gstin, item_key), price in rate_card.items()])
        self.conn.commit()
        return len(items), truth

    def import_purchase_line_items(self, purchase_id: str, *,
                                   supplier_name: str, supplier_gstin: str,
                                   invoice_number: str, invoice_date: str,
                                   items: list[dict]) -> int:
        """
        Record real line items pulled from a CSV/Excel upload or Zoho -
        `items` is a list of {description, quantity_x100, unit_price_paise,
        line_total_paise}, already parsed. Used by merchant/purchase_import.py
        and merchant/zoho.py; this method only ever writes what it is handed,
        never invents a quantity or price.
        """
        import time

        from engine.vendor_terms.rules import normalise_item_key

        business_id = self._scoped()
        now = int(time.time())
        rows = [(f"li_{secrets.token_hex(6)}", purchase_id, business_id,
                 supplier_name, supplier_gstin.strip().upper(), invoice_number,
                 invoice_date, item["description"],
                 normalise_item_key(item["description"]),
                 item["quantity_x100"], item["unit_price_paise"],
                 item["line_total_paise"], now) for item in items]
        self.conn.executemany(
            "INSERT INTO live_purchase_line_items (line_item_id, purchase_id,"
            " business_id, supplier_name, supplier_gstin, invoice_number,"
            " invoice_date, description, item_key, quantity_x100,"
            " unit_price_paise, line_total_paise, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def unreconciled_line_items(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_purchase_line_items WHERE business_id = ?"
            " AND reconciled_run IS NULL ORDER BY recorded_at",
            (self._scoped(),)).fetchall()

    def line_items(self, limit: int = 1_000) -> list:
        return self.conn.execute(
            "SELECT * FROM live_purchase_line_items WHERE business_id = ?"
            " ORDER BY recorded_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    def build_vendor_terms_batch(self, only_unreconciled: bool = True):
        """Assemble real line items into the shape engine.vendor_terms
        already takes, so nothing in engine/vendor_terms had to change to
        run on live data rather than a generated batch."""
        from engine.vendor_terms.detector import LineItem

        rows = (self.unreconciled_line_items() if only_unreconciled
               else self.line_items())
        if not rows:
            return None

        def as_date(text):
            return date.fromisoformat(text) if text else date.today()

        return [LineItem(
            line_item_id=r["line_item_id"], purchase_id=r["purchase_id"],
            supplier_name=r["supplier_name"] or "", supplier_gstin=(
                r["supplier_gstin"] or "").strip().upper(),
            invoice_number=r["invoice_number"] or "",
            invoice_date=as_date(r["invoice_date"]),
            description=r["description"] or "", item_key=r["item_key"] or "",
            quantity_x100=r["quantity_x100"], unit_price_paise=r["unit_price_paise"],
            line_total_paise=r["line_total_paise"]) for r in rows]

    def commit_vendor_terms_run(self, items, source: str = "demo") -> str:
        import time

        run_id = f"vt_{secrets.token_hex(6)}"
        business_id = self._scoped()
        self.conn.execute(
            "INSERT INTO business_vendor_terms_runs (run_id, business_id,"
            " n_items, source, created_at) VALUES (?,?,?,?,?)",
            (run_id, business_id, len(items), source, int(time.time())))
        self.conn.executemany(
            "UPDATE live_purchase_line_items SET reconciled_run = ?"
            " WHERE line_item_id = ?",
            [(run_id, i.line_item_id) for i in items])
        self.conn.commit()
        return run_id

    def record_vendor_terms_findings(self, run_id: str, classified,
                                     decisions, credit_notes=None) -> None:
        import time

        credit_notes = credit_notes or {}
        decision_by_supplier = {d.supplier_gstin: d for d in decisions}
        now = int(time.time())
        rows = []
        for item in classified:
            decision = decision_by_supplier.get(item.supplier_gstin)
            action = decision.action if decision else item.action
            confidence = decision.confidence if decision else 1.0
            decided_by = decision.decided_by if decision else "calculator"
            queued = bool(decision.queued_for_human) if decision else False
            rows.append((
                run_id, self._scoped(), item.line_item_id, item.purchase_id,
                item.supplier_name, item.supplier_gstin, item.invoice_number,
                str(item.invoice_date), item.description, item.quantity_x100,
                item.unit_price_paise, item.contracted_unit_price_paise,
                item.money_at_stake_paise, item.code, action, confidence,
                item.reasoning, decided_by, int(queued),
                credit_notes.get(item.supplier_gstin), now))
        self.conn.executemany(
            "INSERT INTO vendor_terms_findings (run_id, business_id,"
            " line_item_id, purchase_id, supplier_name, supplier_gstin,"
            " invoice_number, invoice_date, description, quantity_x100,"
            " unit_price_paise, contracted_unit_price_paise,"
            " money_at_stake_paise, code, action, confidence, reasoning,"
            " decided_by, queued_for_human, credit_note_text, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def vendor_terms_findings(self, run_id: str, needing_action: bool = False
                              ) -> list:
        sql = ("SELECT * FROM vendor_terms_findings"
              " WHERE business_id = ? AND run_id = ?")
        params = [self._scoped(), run_id]
        if needing_action:
            sql += " AND action != 'none'"
        return self.conn.execute(
            sql + " ORDER BY money_at_stake_paise DESC", params).fetchall()

    def latest_vendor_terms_run(self):
        return self.conn.execute(
            "SELECT * FROM business_vendor_terms_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

    # --- chargeback defence assembler --------------------------------------

    def record_evidence_item(self, dispute_id: str, evidence_type: str,
                             detail: str) -> None:
        import time

        self.conn.execute(
            "INSERT INTO dispute_evidence_items (dispute_id, business_id,"
            " evidence_type, detail, recorded_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT (business_id, dispute_id, evidence_type) DO UPDATE"
            " SET detail = excluded.detail, recorded_at = excluded.recorded_at",
            (dispute_id, self._scoped(), evidence_type, detail.strip(),
             int(time.time())))
        self.conn.commit()

    def dispute_evidence(self, dispute_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM dispute_evidence_items WHERE business_id = ?"
            " AND dispute_id = ? ORDER BY evidence_type",
            (self._scoped(), dispute_id)).fetchall()

    def evidence_by_dispute(self, dispute_ids: list[str]) -> dict[str, set]:
        """dispute_id -> the set of evidence_type keys on file - the shape
        engine.chargeback.detector.detect_batch() wants."""
        if not dispute_ids:
            return {}
        placeholders = ",".join("?" * len(dispute_ids))
        rows = self.conn.execute(
            f"SELECT dispute_id, evidence_type FROM dispute_evidence_items"
            f" WHERE business_id = ? AND dispute_id IN ({placeholders})",
            (self._scoped(), *dispute_ids)).fetchall()
        out: dict[str, set] = {did: set() for did in dispute_ids}
        for r in rows:
            out[r["dispute_id"]].add(r["evidence_type"])
        return out

    def seed_chargeback_demo(self, n: int = 30, seed: Optional[int] = None
                             ) -> tuple[int, dict]:
        """Plant a demo dispute batch and its matching evidence, in one
        call - same trick every other demo seeder in this codebase uses."""
        import time

        from engine.chargeback.generator import generate_disputes

        business_id = self._scoped()
        kwargs = {"n": n}
        if seed is not None:
            kwargs["seed"] = seed
        disputes, evidence_by_dispute, truth = generate_disputes(**kwargs)
        now = int(time.time())

        # OR REPLACE, not a plain INSERT: the demo batch is built from a
        # fixed default seed, so running Demo Mode a second time for the
        # same business regenerates the same dispute_ids - a fresh,
        # unreconciled row each time is what "run it again" should mean,
        # not a UNIQUE-constraint crash. Same fix seed_vendor_terms_demo
        # and seed_gst_filing_demo already apply for the identical reason.
        self.conn.executemany(
            "INSERT OR REPLACE INTO live_disputes (dispute_id, business_id,"
            " payment_id, amount_paise, reason_code, reason_description,"
            " phase, status, respond_by, source, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(d.dispute_id, business_id, d.payment_id, d.amount_paise,
              d.reason_code, d.reason_description, d.phase, d.status,
              d.respond_by, "demo", now) for d in disputes])
        self.conn.executemany(
            "INSERT OR REPLACE INTO dispute_evidence_items (dispute_id,"
            " business_id, evidence_type, detail, recorded_at)"
            " VALUES (?,?,?,?,?)",
            [(did, business_id, etype, "planted demo evidence", now)
             for did, types in evidence_by_dispute.items() for etype in types])
        self.conn.commit()
        return len(disputes), truth

    def record_manual_dispute(self, *, payment_id: str, amount_paise: int,
                              reason_code: str, respond_by: int,
                              reason_description: str = "") -> str:
        """A dispute the merchant typed in themselves - the Without API
        path, since there's no register concept for a chargeback notice
        the way there is for a purchase invoice."""
        import time

        dispute_id = f"disp_manual_{secrets.token_hex(6)}"
        self.conn.execute(
            "INSERT INTO live_disputes (dispute_id, business_id, payment_id,"
            " amount_paise, reason_code, reason_description, phase, status,"
            " respond_by, source, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (dispute_id, self._scoped(), payment_id, amount_paise,
             reason_code.strip(), reason_description.strip(), "chargeback",
             "open", respond_by, "manual", int(time.time())))
        self.conn.commit()
        return dispute_id

    def import_razorpay_disputes(self, raw_items: list) -> dict:
        """Real disputes, pulled from Razorpay - alongside
        seed_chargeback_demo(), never instead of it. Returns
        {"imported": n, "skipped": [(id, reason), ...]}."""
        import time

        from engine.chargeback.razorpay_import import from_razorpay_batch

        disputes, skipped = from_razorpay_batch(raw_items)
        business_id = self._scoped()
        now = int(time.time())

        self.conn.executemany(
            "INSERT OR REPLACE INTO live_disputes (dispute_id, business_id,"
            " payment_id, amount_paise, reason_code, reason_description,"
            " phase, status, respond_by, source, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(d.dispute_id, business_id, d.payment_id, d.amount_paise,
              d.reason_code, d.reason_description, d.phase, d.status,
              d.respond_by, "razorpay", now) for d in disputes])
        self.conn.commit()
        return {"imported": len(disputes), "skipped": skipped}

    def unreconciled_disputes(self) -> list:
        return self.conn.execute(
            "SELECT * FROM live_disputes WHERE business_id = ?"
            " AND reconciled_run IS NULL ORDER BY respond_by",
            (self._scoped(),)).fetchall()

    def disputes(self, limit: int = 1_000) -> list:
        return self.conn.execute(
            "SELECT * FROM live_disputes WHERE business_id = ?"
            " ORDER BY recorded_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    def build_chargeback_batch(self, only_unreconciled: bool = True):
        """Assemble real disputes into the shape engine.chargeback already
        takes, so nothing in engine/chargeback had to change to run on live
        data rather than a generated batch. Returns
        (disputes, evidence_by_dispute) or None if there is nothing to check."""
        from engine.chargeback.detector import Dispute

        rows = (self.unreconciled_disputes() if only_unreconciled
               else self.disputes())
        if not rows:
            return None

        disputes = [Dispute(
            dispute_id=r["dispute_id"], payment_id=r["payment_id"] or "",
            amount_paise=r["amount_paise"], reason_code=r["reason_code"] or "",
            reason_description=r["reason_description"] or "",
            phase=r["phase"] or "", status=r["status"] or "",
            respond_by=r["respond_by"]) for r in rows]
        evidence = self.evidence_by_dispute([d.dispute_id for d in disputes])
        return disputes, evidence

    def commit_chargeback_run(self, disputes, source: str = "demo") -> str:
        import time

        run_id = f"cb_{secrets.token_hex(6)}"
        business_id = self._scoped()
        self.conn.execute(
            "INSERT INTO business_chargeback_runs (run_id, business_id,"
            " n_disputes, source, created_at) VALUES (?,?,?,?,?)",
            (run_id, business_id, len(disputes), source, int(time.time())))
        self.conn.executemany(
            "UPDATE live_disputes SET reconciled_run = ? WHERE dispute_id = ?",
            [(run_id, d.dispute_id) for d in disputes])
        self.conn.commit()
        return run_id

    def record_chargeback_findings(self, run_id: str, classified, decisions,
                                   evidence_packs=None) -> None:
        import json
        import time

        evidence_packs = evidence_packs or {}
        decision_by_id = {d.dispute_id: d for d in decisions}
        now = int(time.time())
        rows = []
        for d in classified:
            decision = decision_by_id.get(d.dispute_id)
            action = decision.action if decision else d.action
            confidence = decision.confidence if decision else 1.0
            decided_by = decision.decided_by if decision else "calculator"
            queued = bool(decision.queued_for_human) if decision else False
            reasoning = (decision.case_reasoning if decision and
                        decision.case_reasoning else d.reasoning)
            pack = evidence_packs.get(d.dispute_id)
            rows.append((
                run_id, self._scoped(), d.dispute_id, d.reason_code,
                d.amount_paise, d.respond_by, d.code, action, confidence,
                reasoning, decided_by, int(queued),
                json.dumps(pack) if pack else None, now))
        self.conn.executemany(
            "INSERT INTO chargeback_findings (run_id, business_id,"
            " dispute_id, reason_code, amount_paise, respond_by, code,"
            " action, confidence, reasoning, decided_by, queued_for_human,"
            " evidence_pack_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def chargeback_findings(self, run_id: str, needing_action: bool = False
                            ) -> list:
        sql = ("SELECT * FROM chargeback_findings"
              " WHERE business_id = ? AND run_id = ?")
        params = [self._scoped(), run_id]
        if needing_action:
            sql += " AND action != 'none'"
        return self.conn.execute(
            sql + " ORDER BY respond_by ASC", params).fetchall()

    def latest_chargeback_run(self):
        return self.conn.execute(
            "SELECT * FROM business_chargeback_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self._scoped(),)).fetchone()

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
                        captured_at: Optional[int] = None,
                        behaviour=None) -> str:
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

        # `behaviour` overrides the business-wide switch for THIS payment.
        # A real batch is not uniformly wrong: one gateway misprices UPI while
        # charging cards correctly, and a demo where every row carries the
        # same fault teaches the auditor nothing about telling faults apart.
        acting = behaviour if behaviour is not None else self.behaviour()
        result = capture(order["amount"], method, acting,
                         card_network, card_type, is_international, self._rng)
        payment_id = _rzp_id(self._rng, "pay_")

        self.conn.execute(
            "INSERT INTO live_payments (payment_id, business_id, order_id, amount,"
            " method, card_network, card_type, is_international, upi_reference,"
            " fee, tax, behaviour, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payment_id, self._scoped(), order_id, order["amount"], result.method,
             result.card_network, result.card_type, int(is_international),
             result.upi_reference, result.fee, result.tax,
             str(acting),
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
            " p.refunded, p.settled_run_id, p.behaviour"
            " FROM live_orders o LEFT JOIN live_payments p"
            " ON p.order_id = o.order_id WHERE o.business_id = ?"
            " ORDER BY o.created_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    # --- settlement -------------------------------------------------------

    def build_settlement(self, rate_card: dict,
                         delay_working_days: int = 0) -> Optional[Batch]:
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
        # delay_working_days is the gateway missing its own promise. Without
        # it every settlement made here lands exactly on T+2, so the payout
        # timing auditor reading these settlements would correctly and
        # uselessly report that nothing was ever late.
        settled_at = int(add_working_days(
            datetime.fromtimestamp(latest, timezone.utc),
            SETTLEMENT_WORKING_DAYS + max(0, delay_working_days)).timestamp())

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
                          via_batch: bool = False,
                          source: str = "simulator") -> str:
        """
        Persist the batch as a run and mark its payments settled.

        `source` records where the run came from. Without it an import and a
        simulated settlement are indistinguishable once stored - both carry
        seed 0 - and a re-import cannot tell which earlier runs it replaces.
        """
        run_id = self.store.save_run(batch, model=model, effort=effort,
                                     via_batch=via_batch)
        self.conn.execute("UPDATE runs SET source = ? WHERE run_id = ?",
                          (source, run_id))
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

    # Everything a settlement run owns. Listed rather than discovered, so
    # adding a table is a deliberate decision about what replacing an import
    # takes with it - the same reasoning Businesses.OWNED_TABLES uses.
    RUN_OWNED_TABLES = ("payments", "settlement_lines", "bank_credits",
                        "refunds", "tds_entries", "rate_card", "variances",
                        "audit_log", "business_runs")

    def replace_imported_settlements(self, batch, source: str = "razorpay"):
        """
        Commit an import, replacing the last one from the same source.

        A re-import is a fresh snapshot of the same Razorpay account, not more
        money - so it must correct rather than double, which is the rule this
        codebase already applies to purchase registers and recon sources.
        Stacking them instead is what made three imports of twelve payments
        read as thirty-six sales.

        Only imports from THIS source are replaced. Simulator runs are a
        different account of what happened and are left alone, as are imports
        a person has already made decisions on - the check below refuses to
        discard those rather than quietly taking someone's reviewed findings
        with it.
        """
        previous = [
            r["run_id"] for r in self.conn.execute(
                "SELECT r.run_id FROM runs r JOIN business_runs br"
                " ON br.run_id = r.run_id"
                " WHERE br.business_id = ? AND r.source = ?",
                (self._scoped(), source)).fetchall()]

        reviewed = {
            r["run_id"] for r in self.conn.execute(
                "SELECT DISTINCT run_id FROM variances"
                " WHERE human_reviewed = 1").fetchall()} if previous else set()

        run_id = self.commit_settlement(batch, source=source)

        for old in previous:
            if old in reviewed:
                continue                # somebody acted on it; it is evidence
            for table in self.RUN_OWNED_TABLES:
                self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?",
                                  (old,))
            self.conn.execute("DELETE FROM runs WHERE run_id = ?", (old,))
        self.conn.commit()
        return run_id

    # What a realistic month looks like across the rails a small Indian
    # merchant actually sees. Weighted, not uniform: UPI dominates, Amex and
    # international are rare, which is what makes a mixed batch look like a
    # month rather than a test matrix.
    BATCH_INSTRUMENTS = (
        ("upi", 26), ("visa_debit", 14), ("visa_credit", 9),
        ("rupay_debit", 6), ("netbanking", 5), ("wallet", 4),
        ("amex", 3), ("international", 3),
    )

    def generate_mixed_batch(self, n: int = 60, seed: Optional[int] = None,
                             behaviours=None) -> dict:
        """
        A month of sales across several rails, with several DIFFERENT faults
        planted in it - not one behaviour applied uniformly.

        A batch where every payment is wrong the same way proves only that the
        auditor can apply one rule. Mixing them is what shows it telling faults
        apart: a UPI payment priced as a card sits next to a card charged above
        its slab, next to GST computed on the sale value, next to rows that are
        simply correct and must stay quiet.

        Each fault is applied only to instruments it can actually occur on -
        zero-MDR violations need a zero-MDR rail, a mislabel needs UPI - so
        nothing here plants a finding the rules could not legitimately raise.
        """
        import random as _random

        from merchant.gateway import Behaviour

        rng = _random.Random(seed if seed is not None else 20260905)

        # Only the faults the merchant ticked. CORRECT is always available:
        # a batch with no clean rows in it teaches nothing about when the
        # auditor should stay quiet, which is half of what it is for.
        wanted = set(behaviours) if behaviours else None

        # (behaviour, which instruments it can apply to, share of the batch)
        recipes = [
            (Behaviour.CORRECT, [i for i, _w in self.BATCH_INSTRUMENTS], 55),
            (Behaviour.CARD_RATE_ON_UPI, ["upi", "rupay_debit"], 13),
            (Behaviour.OVER_CONTRACT,
             ["visa_debit", "visa_credit", "amex", "netbanking", "wallet",
              "international"], 13),
            (Behaviour.GST_ON_SALE_VALUE,
             [i for i, _w in self.BATCH_INSTRUMENTS], 9),
            (Behaviour.MISLABEL_UPI, ["upi"], 10),
        ]

        if wanted is not None:
            recipes = [r for r in recipes
                       if r[0] in wanted or r[0] is Behaviour.CORRECT]
            # Ticking nothing but "charge correctly" is a legitimate demo -
            # the clean sheet - so an empty fault list is not an error.
            if not recipes:
                recipes = [(Behaviour.CORRECT,
                            [i for i, _w in self.BATCH_INSTRUMENTS], 100)]

        pool, weights = zip(*self.BATCH_INSTRUMENTS)
        made = []
        for _ in range(max(1, n)):
            behaviour, allowed, _share = rng.choices(
                recipes, weights=[r[2] for r in recipes])[0]
            choices = [i for i in allowed if i in pool] or list(pool)
            instrument = rng.choices(
                choices, weights=[weights[pool.index(i)] for i in choices])[0]

            # Ticket sizes that straddle the Rs 2,000 debit-cap boundary, so
            # rules 3 and 4 both get exercised rather than only whichever the
            # amounts happened to fall under.
            rupees = rng.choice([180, 340, 520, 890, 1_240, 1_650, 1_980,
                                 2_400, 3_100, 4_750, 6_200, 9_900])
            method, network, card_type, intl = INSTRUMENT_SHAPES[instrument]
            order_id = self.create_order(rupees * 100, f"Sale {len(made) + 1}")
            self.capture_payment(order_id, method, network, card_type, intl,
                                 behaviour=behaviour)
            made.append((instrument, str(behaviour)))

        from collections import Counter
        return {"n": len(made),
                "instruments": dict(Counter(i for i, _b in made)),
                "faults": dict(Counter(b for _i, b in made))}

    def delete_sale(self, payment_id: str) -> bool:
        """
        Remove one simulated sale and its order.

        Deliberately does NOT reach into a settlement that already included
        it. `payments`/`settlement_lines` are a snapshot of what the gateway
        paid out on a given day; deleting the sale afterwards does not unmake
        that batch, and quietly editing a settled run to match would rewrite
        history the auditor has already read. Delete the settlement too if
        that is what you meant.
        """
        row = self.conn.execute(
            "SELECT order_id FROM live_payments"
            " WHERE payment_id = ? AND business_id = ?",
            (payment_id, self._scoped())).fetchone()
        self.conn.execute(
            "DELETE FROM live_payments WHERE payment_id = ? AND business_id = ?",
            (payment_id, self._scoped()))
        if row:
            self.conn.execute(
                "DELETE FROM live_orders WHERE order_id = ? AND business_id = ?",
                (row["order_id"], self._scoped()))
        self.conn.commit()
        return row is not None

    def delete_all_sales(self) -> int:
        """Every simulated sale and order this business holds."""
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM live_orders WHERE business_id = ?",
            (self._scoped(),)).fetchone()["n"]
        self.conn.execute("DELETE FROM live_payments WHERE business_id = ?",
                          (self._scoped(),))
        self.conn.execute("DELETE FROM live_orders WHERE business_id = ?",
                          (self._scoped(),))
        self.conn.commit()
        return n

    def delete_run(self, run_id: str) -> bool:
        """
        Remove one settlement and everything hanging off it.

        Scoped through business_runs, so one business cannot delete another's.
        Deliberately takes the findings with it: a variance is a statement
        about rows that no longer exist, and leaving it behind would put a
        recoverable figure on the Home page pointing at nothing.
        """
        if not self.owns_run(run_id):
            return False
        for table in self.RUN_OWNED_TABLES:
            self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        # The live payments this settled go back to unsettled rather than
        # vanishing - deleting a settlement undoes the settling, not the sale.
        self.conn.execute(
            "UPDATE live_payments SET settled_run_id = NULL"
            " WHERE settled_run_id = ? AND business_id = ?",
            (run_id, self._scoped()))
        self.conn.commit()
        return True

    def delete_payment_from_run(self, run_id: str, payment_id: str) -> bool:
        """
        Drop one transaction out of a settlement.

        Its findings go too, for the same reason delete_run takes them: a
        finding about a payment that is no longer in the batch is a claim
        about nothing. The run's record count is corrected so the header does
        not go on claiming a payment it no longer holds.
        """
        if not self.owns_run(run_id):
            return False
        for table in ("payments", "settlement_lines", "variances", "refunds"):
            self.conn.execute(
                f"DELETE FROM {table} WHERE run_id = ? AND payment_id = ?"
                if table != "payments" else
                "DELETE FROM payments WHERE run_id = ? AND payment_id = ?",
                (run_id, payment_id))
        self.conn.execute(
            "UPDATE runs SET n_records = ("
            "  SELECT COUNT(*) FROM payments WHERE run_id = ?)"
            " WHERE run_id = ?", (run_id, run_id))
        self.conn.execute(
            "UPDATE live_payments SET settled_run_id = NULL"
            " WHERE payment_id = ? AND business_id = ?",
            (payment_id, self._scoped()))
        self.conn.commit()
        return True

    def delete_all_runs(self) -> int:
        """Every settlement this business holds. Returns how many went."""
        ids = [r["run_id"] for r in self.settlements()]
        for run_id in ids:
            self.delete_run(run_id)
        return len(ids)

    def imported_payments(self, limit: int = 200) -> list:
        """
        The transactions themselves, newest first - what the gateway said
        happened, before anything audits it.

        Deduplicated by payment_id, keeping the most recent import. Pressing
        Import twice does not mean the money arrived twice: each run is a
        fresh snapshot of the same account, so counting rows rather than
        payments turned twelve transactions into thirty-six.
        """
        return self.conn.execute(
            "SELECT p.payment_id, p.amount, p.method, p.card_network,"
            "       p.card_type, p.is_international, p.upi_reference,"
            "       p.created_at, sl.fee, sl.tax, sl.settled_at,"
            "       MAX(r.created_at) AS imported_at"
            " FROM payments p"
            " JOIN business_runs br ON br.run_id = p.run_id"
            " JOIN runs r ON r.run_id = p.run_id"
            " LEFT JOIN settlement_lines sl ON sl.run_id = p.run_id"
            "   AND sl.payment_id = p.payment_id AND sl.type = 'payment'"
            " WHERE br.business_id = ?"
            " GROUP BY p.payment_id"
            " ORDER BY p.created_at DESC LIMIT ?",
            (self._scoped(), limit)).fetchall()

    def imported_payment_count(self) -> int:
        """Distinct transactions, not rows. See imported_payments."""
        return self.conn.execute(
            "SELECT COUNT(DISTINCT p.payment_id) n FROM payments p"
            " JOIN business_runs br ON br.run_id = p.run_id"
            " WHERE br.business_id = ?", (self._scoped(),)).fetchone()["n"]

    def owns_run(self, run_id: str) -> bool:
        """One business must not be able to open another's settlement by id."""
        return self.conn.execute(
            "SELECT 1 FROM business_runs WHERE run_id = ? AND business_id = ?",
            (run_id, self._scoped())).fetchone() is not None

    def gateway_fee_credit(self) -> dict:
        """
        GST this business has paid to Razorpay on gateway fees, verified
        correct by the settlement audit - the fourth cross-agent connection,
        and a different shape from the other three.

        The other three are an agent, mid-judgment, asking another agent's
        findings about the SAME record. There is no equivalent "same record"
        here: a purchase from a supplier and a payment from a customer share
        nothing to look up by id. What connects them is a fact, not a
        lookup - Razorpay is itself a supplier, GST charged on its fee is
        input credit exactly like GST paid to anyone else, and nothing in
        the purchase register the GST reconciler audits has ever heard of
        it. Surfacing a fact needs no judgment, so this is a calculator-only
        query, not a tool an agent calls.

        Scoped to CLEAN and ROUNDING findings only - the same "safe to
        claim" standard the ITC page already applies to everything else on
        it. A fee under dispute carries a disputed tax figure on top of it;
        claiming credit on a number that might still change is exactly the
        mistake claiming credit exists to avoid.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(v.actual_tax), 0) paise"
            " FROM variances v JOIN business_runs br ON br.run_id = v.run_id"
            " WHERE br.business_id = ?"
            " AND v.exception_code IN ('CLEAN', 'ROUNDING')",
            (self._scoped(),)).fetchone()

        from engine.gst import rules

        return {"paise": row["paise"], "display": rules.rupees(row["paise"]),
                "count": row["n"]}

    def dashboard_summary(self) -> dict:
        """
        The cross-run totals the Home page's hero card needs, in one call -
        gross sales through gateway deductions to what actually landed, plus
        the two GST ITC figures the side panel shows alongside them.

        Every settlement-side figure is summed across EVERY run this
        business has, not one run at a time - `payments`/`settlement_lines`/
        `bank_credits` (engine/store.py) carry no business_id of their own,
        only run_id, so each join goes through business_runs exactly the way
        gateway_fee_credit() above already does. `itc_findings` carries
        business_id directly, no join needed.

        `recoverable_paise` is the same `action = 'dispute'` formula
        Store.totals() already computes one run at a time, widened here to
        every run - the Home page used to get this by summing that per-run
        figure in a loop; this is the same number from one query instead.
        """
        # One row per payment, not per import. Re-importing the same
        # Razorpay account is a fresh snapshot of the same money, not more of
        # it, and summing every row read three imports of twelve payments as
        # thirty-six sales - overstating this merchant's gross by Rs 10,600.
        gross = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) paise FROM ("
            "  SELECT p.payment_id, MAX(p.amount) amount FROM payments p"
            "  JOIN business_runs br ON br.run_id = p.run_id"
            "  WHERE br.business_id = ? GROUP BY p.payment_id)",
            (self._scoped(),)).fetchone()["paise"]

        # Same reasoning for what was deducted: one settlement line per
        # entity, so a re-imported fee is not charged twice on this screen.
        deducted = self.conn.execute(
            "SELECT COALESCE(SUM(fee), 0) fee, COALESCE(SUM(tax), 0) tax FROM ("
            "  SELECT sl.entity_id, MAX(sl.fee) fee, MAX(sl.tax) tax"
            "  FROM settlement_lines sl JOIN business_runs br"
            "  ON br.run_id = sl.run_id"
            "  WHERE br.business_id = ? AND sl.type = 'payment'"
            "  GROUP BY sl.entity_id)",
            (self._scoped(),)).fetchone()

        credited = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) paise FROM ("
            "  SELECT bc.utr, MAX(bc.amount) amount FROM bank_credits bc"
            "  JOIN business_runs br ON br.run_id = bc.run_id"
            "  WHERE br.business_id = ? GROUP BY bc.utr)",
            (self._scoped(),)).fetchone()["paise"]

        recoverable = self.conn.execute(
            "SELECT COALESCE(SUM(v.money_at_stake), 0) paise FROM variances v"
            " JOIN business_runs br ON br.run_id = v.run_id"
            " WHERE br.business_id = ? AND v.action = 'dispute'",
            (self._scoped(),)).fetchone()["paise"]

        from engine.gst.taxonomy import AT_RISK, NO_ACTION

        itc_safe = self.conn.execute(
            "SELECT COALESCE(SUM(claimed_tax), 0) paise FROM itc_findings"
            f" WHERE business_id = ? AND exception_code IN"
            f" ({','.join('?' * len(NO_ACTION))})",
            (self._scoped(), *(str(c) for c in NO_ACTION))).fetchone()["paise"]

        itc_at_risk = self.conn.execute(
            "SELECT COALESCE(SUM(money_at_stake), 0) paise FROM itc_findings"
            f" WHERE business_id = ? AND exception_code IN"
            f" ({','.join('?' * len(AT_RISK))})",
            (self._scoped(), *(str(c) for c in AT_RISK))).fetchone()["paise"]

        # --- the counting cards: Transactions, Customers, Vendors ----------
        #
        # Each is a real count off a real table, never a derived-looking
        # number with nothing behind it. Where a business has never used the
        # agent that owns a table, the count is simply 0 and the card says
        # so - see views.stat_card()'s empty branch.

        txn = self.conn.execute(
            "SELECT COUNT(DISTINCT p.payment_id) n,"
            "       COUNT(DISTINCT p.method) methods FROM payments p"
            " JOIN business_runs br ON br.run_id = p.run_id"
            " WHERE br.business_id = ?", (self._scoped(),)).fetchone()

        # Method mix drives the Payments card's little distribution strip.
        method_mix = [
            (r["method"] or "unknown", r["n"]) for r in self.conn.execute(
                "SELECT p.method, COUNT(DISTINCT p.payment_id) n FROM payments p"
                " JOIN business_runs br ON br.run_id = p.run_id"
                " WHERE br.business_id = ? GROUP BY p.method"
                " ORDER BY n DESC", (self._scoped(),)).fetchall()]

        # Customers come off the sales-invoice side (the GST output-tax
        # agent's own source table), which is the only place this product
        # ever learns a buyer's identity - `payments` carries no buyer at all.
        cust = self.conn.execute(
            "SELECT COUNT(DISTINCT buyer_name) n,"
            " COUNT(DISTINCT CASE WHEN COALESCE(buyer_gstin, '') <> ''"
            "   THEN buyer_name END) registered"
            " FROM live_sale_invoices WHERE business_id = ?",
            (self._scoped(),)).fetchone()

        vend = self.conn.execute(
            "SELECT COUNT(DISTINCT supplier_gstin) n,"
            " COALESCE(SUM(CASE WHEN code = 'OVERBILLED'"
            "   THEN money_at_stake_paise ELSE 0 END), 0) overbilled"
            " FROM vendor_terms_findings WHERE business_id = ?",
            (self._scoped(),)).fetchone()

        # The same figures again, split by where the data came from. The
        # waterfall shows one bar per stage; without this a merchant cannot
        # tell whether the money on screen is their gateway's or the
        # simulator's, which on a page mixing both is the first thing they
        # need to know.
        by_source = {}
        for row in self.conn.execute(
                "SELECT COALESCE(r.source, 'simulator') src,"
                "       COALESCE(SUM(amount), 0) gross, COUNT(*) n FROM ("
                "  SELECT p.payment_id AS pid, p.run_id AS rid,"
                "         MAX(p.amount) AS amount"
                "  FROM payments p JOIN business_runs br ON br.run_id = p.run_id"
                "  WHERE br.business_id = ? GROUP BY p.payment_id) u"
                " JOIN runs r ON r.run_id = u.rid GROUP BY src",
                (self._scoped(),)):
            by_source[row["src"]] = {"gross_paise": row["gross"],
                                     "payment_count": row["n"]}

        fee_paise, tax_paise = deducted["fee"], deducted["tax"]
        return {
            "by_source": by_source,
            "gross_paise": gross, "fee_paise": fee_paise, "tax_paise": tax_paise,
            "net_paise": gross - fee_paise - tax_paise,
            "bank_credited_paise": credited,
            "recoverable_paise": recoverable,
            "itc_safe_paise": itc_safe, "itc_at_risk_paise": itc_at_risk,
            "payment_count": txn["n"], "method_count": txn["methods"],
            "method_mix": method_mix,
            "customer_count": cust["n"],
            "customer_registered": cust["registered"],
            "vendor_count": vend["n"],
            "vendor_overbilled_paise": vend["overbilled"],
        }

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
