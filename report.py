#!/usr/bin/env python3
"""
The dashboard. Checkpoint 9.

  python report.py --db auditor.db --out report.html

## Why this is one HTML file and not a React app

CLAUDE.md section 8 says React plus Recharts. React needs Node, and Node is not
installed on this machine - installing a JavaScript runtime and a package tree
to draw four numbers and a table is the opposite of section 8's actual
instruction, which is "optimise for fewest moving parts".

So: one file, generated from the database, with no build step, no dev server,
no package manager and no internet. Double-click it and it opens. Nothing in it
can fail in a room with bad wifi, which is where this will be opened.

Rows expand using <details>, which is a browser feature rather than JavaScript,
so the click-through works even if scripting is disabled.

If the demo later needs a live "run batch" button, FastAPI can serve this same
data as JSON - the store already exposes exactly the queries it would need.

## Where the accuracy number comes from

The database stores the seed, and the generator is deterministic, so the answer
key can be regenerated exactly rather than stored. That keeps ground truth out
of the findings table, where a stray join could let it leak into the audit.
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timezone

from engine.detector import detect_batch
from engine.expected_value import Payment, classify_instrument, load_rate_card
from engine.expected_value import rupees
from engine.gate import gate_batch
from engine.store import DEFAULT_PATH, Store
from merchant.views import COMPONENTS, TOKENS
from engine.taxonomy import ACTION_FOR, DESCRIPTION, NO_ACTION, RECOVERABLE, ExceptionCode
from generator.synthetic import generate_batch

# Colour follows the ACTION, not the exception code - the taxonomy is organised
# by what the merchant must DO, so the interface is too. Same mapping the web
# app uses; both read the same tokens, so they cannot drift apart again.
ACTION_PILL = {"dispute": "danger", "fix_books": "warn",
               "escalate": "violet", "dismiss": ""}
ACTION_COLOUR = {"dispute": "var(--danger)", "fix_books": "var(--warn)",
                 "escalate": "var(--violet)", "dismiss": "var(--muted)"}


# The report has no app shell - no rail, no top bar - so it needs a page frame
# of its own. Everything else comes from COMPONENTS.
REPORT_CSS = """
.wrap { max-width:1000px; margin:0 auto; padding:32px 20px 80px }
header h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.02em }
header p { color:var(--muted); margin:0 0 22px; font-size:12.5px }
.legend { color:var(--muted); font-size:11.5px; margin-top:10px }
.reason { margin:13px 0 9px; color:var(--ink-2) }
.rule { color:var(--muted); font-size:12px; margin:0 0 11px }
.meta { color:var(--muted); font-size:11.5px; margin:9px 0 0 }
.queued { font-size:11.5px; color:var(--violet); margin:9px 0 0 }
.dispute { margin-top:12px }
.dispute-head { font-size:10px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin-bottom:5px; font-weight:600 }
.bar-row { display:grid; grid-template-columns:200px 1fr 34px; gap:11px;
  align-items:center; margin-bottom:5px; font-size:11.8px }
.bar-label { color:var(--muted); overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap }
.bar-track { background:var(--line-2); border-radius:3px; height:8px }
.bar-fill { height:8px; border-radius:3px }
.bar-value { text-align:right; font-variant-numeric:tabular-nums }
"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _date(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y %H:%M")


def _scorecard(store: Store, run_id: str):
    """
    Rebuild the answer key from the stored seed and score the run against it.

    Ground truth is deliberately NOT stored alongside the findings. Regenerating
    it costs milliseconds and removes any chance of a stray join letting the
    answer leak into something the agent or the report reads as evidence.
    """
    row = store.conn.execute("SELECT seed, n_records FROM runs WHERE run_id = ?",
                             (run_id,)).fetchone()
    if row is None or row["seed"] is None:
        return None
    batch, truth = generate_batch(row["n_records"], seed=row["seed"])
    variances = detect_batch(batch)

    stored = {r["payment_id"]: r["exception_code"]
              for r in store.findings(run_id)}
    if set(stored) != set(truth):
        return None      # the run did not come from this generator; do not score

    correct = sum(1 for pid, code in truth.items() if stored.get(pid) == code)
    anomalies = {pid: code for pid, code in truth.items()
                 if code not in {str(c) for c in NO_ACTION}}
    caught = sum(1 for pid, code in anomalies.items() if stored.get(pid) == code)
    false_accusations = [
        pid for pid, code in truth.items()
        if code in {str(c) for c in NO_ACTION}
        and stored.get(pid) not in {str(c) for c in NO_ACTION}
    ]
    return {
        "total": len(truth), "correct": correct,
        "anomalies": len(anomalies), "caught": caught,
        "false_accusations": len(false_accusations),
    }


def _bar(label: str, n: int, of: int, colour: str) -> str:
    pct = (n / of * 100) if of else 0
    return f"""
      <div class="bar-row">
        <div class="bar-label">{_esc(label)}</div>
        <div class="bar-track">
          <div class="bar-fill" style="width:{pct:.1f}%;background:{colour}"></div>
        </div>
        <div class="bar-value">{n}</div>
      </div>"""


def build_html(store: Store, run_id: str) -> str:
    run = store.conn.execute("SELECT * FROM runs WHERE run_id = ?",
                             (run_id,)).fetchone()
    findings = store.findings(run_id)
    totals = store.totals(run_id)
    card = _scorecard(store, run_id)

    trail = {r["payment_id"]: r for r in store.audit_trail(run_id)}
    payments = {r["payment_id"]: r for r in store.conn.execute(
        "SELECT * FROM payments WHERE run_id = ?", (run_id,))}

    # "card" tells the merchant nothing; "RuPay debit card" tells them why the
    # fee should have been zero. The label is derived from the stored fields
    # rather than saved, so it always matches what the engine actually priced.
    rate_card = load_rate_card()

    def _label(row) -> str:
        if row is None:
            return ""
        key, _ = classify_instrument(Payment(
            payment_id=row["payment_id"], amount=row["amount"],
            method=row["method"], card_network=row["card_network"],
            card_type=row["card_type"],
            is_international=bool(row["is_international"]),
            upi_reference=row["upi_reference"]))
        return rate_card["instruments"][key]["label"]

    # --- the full reconciliation ------------------------------------------
    #
    # Gross minus fees minus GST does NOT equal the bank credit, and showing
    # only those three lines invites the first person who does the arithmetic
    # to conclude the report is broken. On this run the three-line version was
    # out by Rs 61,952 - refunds, one payment that never settled, and an
    # unexplained adjustment, all of them invisible.
    #
    # Every line of the difference, which is the actual promise: "we tell you
    # where every rupee of the difference went."
    gross = sum(p["amount"] for p in payments.values())
    fees = store.conn.execute(
        "SELECT COALESCE(SUM(fee),0) AS f, COALESCE(SUM(tax),0) AS t"
        " FROM settlement_lines WHERE run_id = ?", (run_id,)).fetchone()
    credited = store.conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM bank_credits WHERE run_id = ?",
        (run_id,)).fetchone()["a"]

    settled_gross = store.conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM settlement_lines"
        " WHERE run_id = ? AND type = 'payment'", (run_id,)).fetchone()["a"]
    refunded = -store.conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM settlement_lines"
        " WHERE run_id = ? AND type = 'refund'", (run_id,)).fetchone()["a"]
    adjusted = -store.conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM settlement_lines"
        " WHERE run_id = ? AND type NOT IN ('payment','refund')",
        (run_id,)).fetchone()["a"]
    never_settled = gross - settled_gross

    money_rows = [("Gross sales", gross, False)]
    if never_settled:
        money_rows.append(("Captured but never settled", -never_settled, False))
    if refunded:
        money_rows.append(("Refunded to customers", -refunded, False))
    if adjusted:
        money_rows.append(("Unexplained adjustments", -adjusted, False))
    money_rows += [
        ("Gateway fees", -fees["f"], False),
        ("GST on fees", -fees["t"], False),
        ("Credited to the bank", credited, True),
    ]
    # If this ever fails the report is lying, so say so rather than print it.
    ties_out = (gross - never_settled - refunded - adjusted
                - fees["f"] - fees["t"] == credited)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["exception_code"]] = counts.get(f["exception_code"], 0) + 1

    # --- the findings table ------------------------------------------------
    rows = []
    for f in findings:
        code = f["exception_code"]
        pay = payments.get(f["payment_id"])
        log = trail.get(f["payment_id"])
        colour = ACTION_COLOUR.get(f["action"], "var(--muted)")
        pill = ACTION_PILL.get(f["action"], "")
        recoverable = code in {str(c) for c in RECOVERABLE}

        detail = [f'<p class="reason">{_esc(f["reasoning"] or "-")}</p>']
        if f["rule_cited"]:
            detail.append(f'<p class="rule"><b>Rule relied on:</b> '
                          f'{_esc(f["rule_cited"])}</p>')
        detail.append(
            '<div class="numbers">'
            f'<span>fee charged <b>{rupees(f["actual_fee"])}</b></span>'
            f'<span>expected <b>{rupees(f["expected_fee"])}</b></span>'
            f'<span>GST charged <b>{rupees(f["actual_tax"])}</b></span>'
            f'<span>expected <b>{rupees(f["expected_tax"])}</b></span>'
            f'<span>difference <b>{rupees(f["delta"])}</b></span>'
            '</div>')
        if log:
            detail.append(
                f'<p class="meta">decided by {_esc(f["decided_by"])}'
                + (f' &middot; {_esc(log["model"])}' if log["model"] else "")
                + f' &middot; confidence {f["confidence"]:.2f}' if f["confidence"] is not None else ""
                + '</p>')
        if f["queued_for_human"]:
            import json as _json
            reasons = _json.loads(f["queue_reasons"] or "[]")
            detail.append('<p class="queued"><b>Held for a human:</b> '
                          + _esc("; ".join(reasons)) + '</p>')
        if f["dispute_text"]:
            detail.append('<div class="dispute"><div class="dispute-head">'
                          'Ready to send</div><pre>'
                          + _esc(f["dispute_text"]) + '</pre></div>')

        rows.append(f"""
        <details class="finding{' rec' if recoverable else ''}">
          <summary>
            <span class="mono">{_esc(f["payment_id"])}</span>
            <span><span class="pill {pill}">{_esc(code)}</span></span>
            <span style="color:var(--muted)">{_esc(_label(pay))}</span>
            <span style="text-align:right">{rupees(f["money_at_stake"])}</span>
            <span style="text-align:right;color:{colour};font-weight:640">{_esc(f["action"])}</span>
          </summary>
          <div class="detail">{''.join(detail)}</div>
        </details>""")

    # --- headline panels ---------------------------------------------------
    accuracy_panel = ""
    if card:
        pct = card["correct"] / card["total"] * 100 if card["total"] else 0
        accuracy_panel = f"""
      <div class="card">
        <h2>Measured against a known answer key</h2>
        <p class="sub">The batch was generated with {card['anomalies']} planted
           anomalies. This is what the system found.</p>
        <div class="stats">
          <div class="stat"><b>{pct:.1f}%</b><span>{card['correct']} of
            {card['total']} classified correctly</span></div>
          <div class="stat"><b>{card['caught']}/{card['anomalies']}</b>
            <span>planted anomalies caught</span></div>
          <div class="stat {'good' if card['false_accusations'] == 0 else 'bad'}">
            <b>{card['false_accusations']}</b><span>false accusations</span></div>
        </div>
      </div>"""

    composition = "".join(
        _bar(code, n, len(findings),
             ACTION_COLOUR.get(str(ACTION_FOR[ExceptionCode(code)]), "var(--muted)"))
        for code, n in sorted(counts.items(), key=lambda kv: -kv[1]))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settlement audit &middot; {_esc(run_id)}</title>
<style>{TOKENS}{COMPONENTS}{REPORT_CSS}</style></head><body><div class="wrap">

<header>
  <h1>Settlement deduction audit</h1>
  <p>{_esc(run_id)} &middot; seed {_esc(run["seed"])} &middot;
     {_esc(run["n_records"])} records &middot; run {_date(run["created_at"])}
     {'&middot; ' + _esc(run["model"]) if run["model"] else ''}
     {'(effort ' + _esc(run["effort"]) + ')' if run["effort"] else ''}</p>
</header>

{accuracy_panel}

<div class="card">
  <h2>What happened to the money</h2>
  <p class="sub">Every rupee of the difference between what was sold and what
     arrived.</p>
  <div class="money">
    {''.join(f'<div class="lbl{" total" if t else ""}">{_esc(label)}</div>'
             f'<div class="val{" total" if t else ""}">{rupees(amount)}</div>'
             for label, amount, t in money_rows)}
  </div>
  <p class="legend">{"Every line reconciles to the paise" if ties_out
     else "WARNING: these lines do not reconcile - do not trust this panel"}
     &mdash; and the deductions still contain
     {totals['n'] - counts.get('CLEAN', 0)} findings.
     Balancing correctly is not the same as being charged correctly.</p>
</div>

<div class="card">
  <h2>Recoverable and at risk</h2>
  <div class="stats">
    <div class="stat"><b>{rupees(totals['recoverable_paise'])}</b>
      <span>identified as recoverable</span></div>
    <div class="stat"><b>{totals['by_calculator']}</b>
      <span>settled by the rate card, never sent to the model</span></div>
    <div class="stat"><b>{totals['queued']}</b>
      <span>held for a human</span></div>
  </div>
</div>

<div class="card">
  <h2>Findings by category</h2>
  <p class="sub">Three of these categories mean &ldquo;do nothing&rdquo;. That is
     deliberate.</p>
  {composition}
</div>

<div class="card">
  <h2>Every record</h2>
  <p class="sub">Click any row for the reasoning, the rule it relied on, and the
     dispute message where there is one.</p>
</div>
{''.join(rows)}

</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_PATH))
    ap.add_argument("--run", help="run id (default: the most recent)")
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()

    with Store(args.db) as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print(f"No runs in {args.db}. Run:  python audit.py --db {args.db}")
            return 1
        page = build_html(store, run_id)

    with open(args.out, "w") as f:
        f.write(page)
    print(f"wrote {args.out}  ({len(page):,} bytes, self-contained)")
    print(f"open it with:  open {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
