# ArthAI — Platform Reference

> A record of what has actually been built, as of 31 August 2026. Where
> `CLAUDE.md` is the brief this project was built from, this file is the
> as-built description — what exists, what it does, and where it
> deliberately stops.

---

## 1. The thesis

Every agent on this platform audits the same shape of gap: **something was
agreed or legislated, something else actually happened, and nobody
routinely checks whether they match.**

- A rate card says one fee; the gateway deducted another.
- A supplier's GSTR-1 says one thing; their GSTR-3B payment says another.
- A settlement cycle was promised at T+2; the money arrived on T+9.
- A GSTR-1 says one output-tax liability; the GSTR-3B about to be filed
  says another.

Razorpay (or any gateway) solves *"did the money arrive"* and *"what was
deducted."* Nobody solves *"should it have been deducted."* That is Layer 3
of the settlement problem, and it recurs across enough of Indian merchant
finance that this became a platform instead of a single tool.

## 2. The core architectural rule

> **The LLM never does arithmetic.**

Every agent on this platform is split into two layers:

| Layer | Implemented as | Job |
|---|---|---|
| **Calculator** | Plain Python, `engine/` | Computes what *should* be true. Deterministic, unit-tested, never wrong. |
| **Judge** | Claude agent, `agent/` | Decides *what kind* of gap this is and what to do about it — the part that needs judgment, not arithmetic. |

A calculator can say a fee was ₹610 instead of ₹412. It cannot say whether
that is a pricing error, a mislabelled instrument, an unclaimed refund, or
rounding noise. That second question is judgment, and it is where the
agent earns its place. If a script can answer the question, there is no
agent call for it — several layers in the GST filing system below make
**zero** LLM calls, ever, because nothing in them is actually ambiguous.

A sibling rule, added mid-build and applied retroactively: **the system
never claims official-schema compliance it hasn't verified.** A claim like
"generates the official GSTN JSON" is a verifiable fact, not a vibe — see
§5.6 for what that discipline looks like in practice.

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python | Best fit for the rate/tax logic |
| Storage | SQLite | One file, no server, no ops |
| Agent | Claude Agent SDK (`anthropic`) | Handles the tool-call loop |
| API | FastAPI + `uvicorn` | Minimal |
| UI | Server-rendered HTML (`merchant/views.py`) | No client framework, no build step |
| Money | Integer paise, everywhere | Floats are never used for currency |

Explicitly skipped: Postgres, Docker, Redis, queues, a frontend build
pipeline. Auth exists (email/password, session cookies, owner/operator
roles) but is intentionally lightweight — this is a working prototype, not
a hardened multi-tenant SaaS.

## 4. Running it

```bash
source .venv/bin/activate
uvicorn merchant.app:app --port 8000
```

`.claude/launch.json` has this pre-configured. The database defaults to
`merchant.db` in the repo root; override with the `AUDITOR_DB` environment
variable to point at a scratch file. First account created on a fresh
database becomes the operator (can see every business).

```bash
pytest -q          # 1300 tests, ~50s
```

---

## 5. The agents

Six are **live** — implemented, tested, runnable. Three are **planned** —
declared with a stated reason they aren't built, never mocked. A UI that
renders a working reconciler for something that doesn't exist is "a lie
with a progress bar," so the planned agents render as plainly unavailable
and cannot be run.

### 5.1 Live

| Agent | Answers | Authority |
|---|---|---|
| **Settlement Deduction Auditor** | Which of my gateway's deductions were correct, and which are recoverable? | PSS Act s.10A, RBI/2017-18/105, GST law, Income Tax Act 2025 |
| **GST Input Credit Reconciler** | Which suppliers didn't file, what's it costing me, and what am I claiming that will come back as a notice? | GSTR-2B, s.16(2), Rule 88D |
| **Forward Cash Forecaster** | Will I make payroll on the 14th, and if not, what do I move? | Arithmetic on obligations the merchant already agreed to |
| **Three-Way Reconciliation Agent** | I billed it, the gateway says it settled it — did the bank actually receive it? | No statute — arithmetic and evidence |
| **Payout Timing Auditor** | Is my money arriving on the promised cycle, and what is the float worth? | The merchant agreement's stated settlement cycle |
| **GST Output Tax Reconciler** | What does my outward return say, is it still correctable, and how little cash do I actually owe? | CGST Rule 88C, s.50, s.73/74, the QRMP scheme — see §6 |

Each live agent has a Demo Mode (synthetic data with planted, known
answers — the same batch that produces a measured accuracy claim) and,
where a real integration exists, a real-data path pulling from Razorpay or
an uploaded file.

### 5.2 Planned, and why

| Agent | Would answer | Why it isn't built |
|---|---|---|
| **TDS Credit Tracker** | Was TDS withheld from me, and did it show up as my credit? | Razorpay's settlement report carries no TDS line. The only real documents are a quarterly Form 16A certificate and Form 26AS/168, neither with an API — testing against real data would mean the merchant doing the tool's job by hand first. |
| **Chargeback Defence Assembler** | Which disputes can I actually win, and what do I send? | Real, just not scheduled — short deadlines, per-case paperwork. |
| **Vendor Invoice Auditor** | Am I being billed my contracted rates? | Same incentive problem as the gateway itself: nobody who issues an invoice builds the tool that audits it. |

### 5.3 Business-process view

The same agents, organised by which real workflow they sit in
(`merchant/nav.py`'s `FLOWS`):

```
Income Management:  Sell → Settle(★) → Payout(★) → Refund/Dispute → Reconcile(★) → Report
Vendor Management:   Purchase → Pay → Claim GST Credit(★)
Treasury Management: Forward(★)
GST Management:      File(★)
```
(★ = live agent; unmarked stages are either Razorpay's own plumbing — cash
already correctly moving, nothing to audit — or a planned agent.)

### 5.4 Cross-agent tools

Four live connections stop the agents from being four apps that happen to
share a database (`merchant/cross_agent_tools.py`):

| Tool | Used by | Checks |
|---|---|---|
| `settlement_status(payment_id)` | Cash Forecaster | Does the settlement auditor already know this exact receipt is disputed? |
| `at_risk_input_credit()` | Cash Forecaster | Has the ITC reconciler found claimed credit that may have to be repaid? |
| `recon_status(payment_id)` | Cash Forecaster, Three-Way Reconciler | Did the three-way reconciler already find this settlement never reached the bank? |
| `at_risk_output_tax()` | Cash Forecaster | Has the GST output-tax reconciler found a locked filing period still short of what was paid? |

Each tool opens its own scoped DB connection (the evidence used to build
the current run is already closed by the time a tool call happens),
returns nothing when demo data is being audited (a demo forecast's ids
don't exist in any other agent's real tables — offering the tool would
mean "checked, found nothing" reads as a fact about the money when it's
really just disconnected demo universes), and is read-only, verified by
an actual before/after row-count check in tests, not by a name that
sounds safe.

### 5.5 Guardrails, on every live agent

1. No agent writes to a ledger. It proposes; a human disposes.
2. Every finding carries a reasoning trace, a confidence score, and the
   rule it relied on with that rule's source.
3. Confidence below threshold, or money above a per-business cap, routes
   to a human review queue — never auto-resolved.
4. No agent invents a balancing entry. If something doesn't reconcile, it
   says so.
5. Every decision is timestamped and replayable (`merchant/trace.py`,
   the Activity log).

---

## 6. Deep dive: the GST Output Tax Reconciler

The newest and largest agent — four layers behind one workspace
(`engine/gst_filing/`, `agent/gst_correction_*.py`,
`agent/gst_filing_documents.py`, `merchant/agents/gst_filing.py`), sharing
one subject (a business's outward GST position) rather than four separate
catalogue entries.

### Layer 1 — GSTR-1 assembly (`classifier.py`, `gstn_export.py`)

Classifies outward invoices into B2B / B2CL / B2CS, computes the CGST+SGST
vs IGST split from the buyer's state against the merchant's home state,
aggregates an HSN-wise summary, and flags B2B invoices missing an
e-invoice IRN. **Fully mechanical — zero agent calls, ever**, because
nothing here is actually ambiguous.

Exports the **real GSTN offline-utility JSON shape** (`gstin`, `fp`,
`b2b`/`b2cl`/`b2cs`/`hsn`/`doc_issue`, with the exact field names —
`ctin`, `itms`, `itm_det`, `iamt`/`camt`/`samt`, etc.) and the **real
e-invoice IRN-generation request shape** (`Version`/`TranDtls`/`DocDtls`/
`SellerDtls`/`BuyerDtls`/`ItemList`/`ValDtls`). Both schemas were verified
against two independent real sources this session — a certified GST
Suvidha Provider's API docs and `resilient-tech/india-compliance`, an
open-source GST compliance app used in production — not invented. Fields
this system genuinely has no data for (a buyer's postal address, most
prominently) are named in a `missing_fields` list on every export, never
guessed.

### Layer 2 — GSTR-1A window / DRC-03 lock (`timing.py`)

A state machine: while GSTR-3B for a period is unfiled, a mismatch between
GSTR-1 and what's about to be paid can still be corrected for free via
GSTR-1A. Once GSTR-3B is filed, that table is hard-locked (the July 2025
rule change) and the only route left is a DRC-03 voluntary payment plus
s.50 interest, computed daily, at 18% (ordinary shortfall) or 24%
(wrongly-claimed ITC) — the rate is named by the record, never inferred
from the size of the gap.

**One agent call per run**, and only when there's more than one open
period competing for attention — the judgment is comparative ("which to
file first"), never a change to the mechanical exception code, which the
agent is structurally unable to soften.

### Layer 3 — ITC offset hierarchy / Rule 88C shield (`offset.py`)

Enforces the real utilisation order: IGST credit clears IGST liability
first, spills into CGST then SGST; CGST and SGST credit never cross into
each other. Then applies cash already sitting in the electronic cash
ledger, per head, with no spillover — arriving at the actual minimum new
PMT-06 deposit required, not a naive per-head total.

Separately (same two numbers layer 2 already has, reused rather than
re-derived), checks each locked period against Rule 88C's threshold
(whichever is *lower* of ₹1 lakh or 20% of paid tax). A breach drafts a
DRC-01B reply — the agent writes only the 2–4 sentence connecting
paragraph; every figure and citation is Python-assembled and checked
against invented figures before it's shown. Citations: Rule 88C, s.50,
s.73 (never s.74/fraud, which would need a materially different case). No
CBIC circular exists for this specific notice type, so none is invented —
instead the real, dated authority behind "must be given a chance to
explain before recovery," CBIC Instruction No. 01/2022-GST (7 January
2022), is cited by its own name.

### Layer 4 — QRMP method / IFF / quarterly close (`qrmp.py`)

Eligibility is a turnover comparison against the QRMP ceiling. The
fixed-sum-vs-self-assessment choice compares the quarter's actual
two-month cash totals and picks whichever ties up less cash — a `>=`
comparison, not a judgment. IFF worth-filing is a merchant-set materiality
threshold (own setting, not a per-invoice AI call) applied to month 1 and
2 only — month 3 has no IFF window, it's covered by the quarter's own
GSTR-1. Month 3 itself aggregates all three months' liability into the
real GSTR-3B `sup_details.osup_det` shape and nets it against what was
already paid via PMT-06 in months 1–2, showing the balance due or credit
carried forward.

Fully mechanical — zero agent calls.

### 6.1 Real data, alongside Demo Mode

Beyond the generated demo batch, the Overview tab can pull real outward
invoices from Razorpay's Invoices API (`engine/gst_filing/razorpay_import.py`).
GSTIN, HSN/SAC code and tax rate are real, documented fields on that
endpoint — but Razorpay's own API can only *create* an invoice without
them (a person has to fill them in through the Dashboard), so an imported
invoice missing any of it is classified honestly, not guessed: a missing
GSTIN falls through to the existing B2C rule; a missing HSN code excludes
the invoice from the draft the same way an on-file HSN with no configured
rate already does; an unresolvable place of supply is skipped outright
rather than silently defaulting to intra-state, which would have quietly
mis-split IGST against CGST+SGST. An invoice spanning several HSN codes
splits into one row per code rather than losing data. Layers 2–4 run
exactly the same scenario either way — only layer 1's invoice source
changes.

### 6.2 What every draft says, and doesn't

| Artifact | What it actually is | What it explicitly is not |
|---|---|---|
| GSTR-1 JSON | Real GSTN field shape, verified this session | Not GSTN-accepted — never tested against a live portal upload |
| E-invoice batch | Real INV-01 request shape | Missing fields named, not filled; not a live IRN |
| GSTR-1A | Real `b2csa`-shaped aggregate amendment | Not a per-invoice amendment — layer 2 only knows the gap at the period level |
| DRC-03 / PMT-06 | Rendered HTML draft, real form fields | Filed through the portal's own web form — never a JSON upload, never a submission this tool makes |
| DRC-01B reply | Paste-ready letter, real citations only | No circular invented where none was found |

Nothing here writes to a ledger, files anything, or claims a government
system accepted it. Every screen says so.

### 6.3 Measured accuracy

`engine/gst_filing/scoring.py` scores layers 1 and 2 against the demo
generator's own planted answer key (layers 3–4 are single computations
per run, not classifications with a wrong answer to catch — nothing to
score). On the canonical 40-invoice / 5-period demo batch this reads
100/100 both ways — expected, since both sides are the same deterministic
rules; the number exists to catch a real implementation bug, not to
impress. Narrated live during every demo run: *"40/40 invoices classified
correctly, 10/10 planted anomalies caught."*

---

## 7. Data model

All money is stored in **paise, as integers** — floats are never used for
currency anywhere in this codebase. Key tables (`merchant/ledger.py`,
`engine/store.py`), grouped by what they serve:

- **Settlement**: `payments`, `settlement_lines`, `bank_credits`,
  `variances`, `business_rate_card`
- **GST input credit**: `itc_findings`, `business_itc_runs`,
  `supplier_filing_history`, `resolution_memory`
- **GST output tax**: `live_sale_invoices`, `business_hsn_rate_card`,
  `business_gst_profile`, `business_gstr1_runs`, `gst_filing_cycles`,
  `gst_correction_findings`, `gst_offset_findings`, `gst_qrmp_findings`,
  `live_gst_ledger_balances`, `business_qrmp_settings`
- **Cash forecast / three-way / payout timing**: their own
  `business_*_runs` and `*_findings` tables, same shape throughout
- **Platform**: `businesses`, `users`, `sessions`, `data_sources`
  (Razorpay connection, encrypted secret), `access_log`, `benchmarks`

New columns on an existing table use `_add_column()` (a one-line
`ALTER TABLE`, applied on every startup, a no-op once the column exists) —
`CREATE TABLE IF NOT EXISTS` silently does nothing to a table a database
already has, so a schema change made after a database was first created
needs this or it never reaches that database. This has caught a real bug
three times in this project's history.

## 8. Recurring discipline

A few rules repeat across every agent on this platform, stated once here
rather than in each module:

- **Absence is not innocence.** A record the system has no data for is
  named as missing, never defaulted to a guessed value that happens to
  look plausible. An HSN with no rate on file, a buyer with no GSTIN, a
  buyer with no billing address — all excluded and flagged, never filled
  in.
- **A wrong rule is worse than a missing one.** Numbers not independently
  verified this session (the B2CL threshold, the e-invoicing turnover
  threshold, the QRMP fixed-sum percentage) are named constants with a
  comment saying so — a citation seam, fixed later with a one-line edit,
  not a silent guess presented as settled law.
- **Don't flag everything.** Several exception codes across this platform
  exist specifically to mean "do nothing" — a refund with the original fee
  retained, a period within tolerance, an overpayment. Knowing when *not*
  to alarm someone is as much the product as catching the real gap.
- **A demo never claims more than it can prove.** Every Demo Mode plants
  known errors and hands back the answer key, so a match-rate claim is a
  measurement, not a vibe. A real data pull never gets an accuracy
  number, because there's nothing to measure it against.

---

## 9. Test coverage

**1300 tests**, ~50 seconds, no network calls. Every engine module has its
own unit tests; every agent's output-checking layer (`unverified_figures`,
the guardrail gate, the "never softens the mechanical action" rule) is
tested directly against a mocked model response, not just against real API
calls. Cross-agent tools are tested for the read-only guarantee with an
actual before/after row count, not a name that sounds safe.
