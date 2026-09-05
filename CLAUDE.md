# ArthAI

> Context file for Claude Code. Read this fully before writing any code.
> Project owner is solo, strong on the problem domain, weaker on implementation.
> Explain engineering choices in plain language as you go.

---

## 0. TL;DR

We are building **ArthAI**, an **AI agent that audits every rupee a payment
gateway deducted from a merchant's settlement** — and tells the merchant which
deductions were correct, which were overcharges they can recover, and which put
a tax credit at risk.

**Pitch line:**
> "You got ₹7,370 instead of ₹9,000. We tell you where every rupee of the
> difference went — and which parts you should be angry about."

**Submission:** Razorpay Buildathon, Track 04 — AI Finance Controller.
**Deadline:** 5 September 2026. Solo builder.
**Track's stated bar:** close one finance-ops loop across a 50+ record batch,
report match rate and unresolved exceptions. Throughput + measured accuracy +
honest exception list.

---

## 1. The problem

### 1.1 What happens today

A merchant makes five sales totalling ₹9,000. Two days later, one line appears
in their bank statement:

```
Credit: ₹7,370.20   UTR: HDFCN12345678
```

₹1,629.80 is missing. It is not stolen — it is MDR, GST on MDR, and a refund,
all netted together before the credit was sent. The merchant's job is to
reconstruct that decomposition in reverse.

Razorpay gives them a settlement report to help. The report is honest and
complete. **But it is a statement, not a verification.**

### 1.2 The three layers

| Layer | Question | Status |
|---|---|---|
| 1 | Did the money arrive? | Razorpay solves this (UTR, settlement_id) |
| 2 | What was deducted? | Razorpay solves this (fee, tax, adjustment columns) |
| 3 | **Should it have been deducted?** | **NOBODY HAS BUILT THIS** |

We are building Layer 3.

### 1.3 The technical proof that Layer 3 is a distinct problem

From fee-reconciliation practitioners:

> Net settlement reconciliation confirms the gross-to-net calculation is
> mathematically correct; fee reconciliation confirms that the inputs to that
> calculation were applied according to the contract. **A settlement can balance
> correctly at the net level while still containing fee overcharges that were
> offset by other deductions.**

Razorpay's reconciliation proves the arithmetic. Nothing proves the *rate*.
Those are different jobs and only one is built.

### 1.4 Why this stays unbuilt (the structural argument)

A tool that verifies Razorpay's fees produces exactly one kind of output:
*"Razorpay overcharged you ₹X."*

**No payment company builds the tool that bills itself.** This is an incentive
problem, not a capability problem — which means the gap does not close when
Razorpay gets better engineers. It is permanent.

Use this in the pitch. It is much more durable than "nobody thought of it yet."

### 1.5 Evidence this is real (for the pitch deck)

- **Razorpay merchant, Trustpilot:** "There have been 3 instances in last 2
  months where our customers were charged 8% currency conversion charges, which
  are not supposed to be charged." — Three they *caught*, by hand. Nobody
  catches three in two months and thinks that's all of them.
- **PissedConsumer:** 1.7 stars across ~397 reviews; negative sentiment driven
  by refund and settlement complaints, including amounts deducted but not
  deposited to merchants.
- **Shopify Payments merchant (same pattern, different platform):** accountants
  found total 2024 payouts were ~15% below expected net sales. Standard
  troubleshooting found nothing. Discovered a year late. Still unexplained.
- **Razorpay's own settlement-transparency blog** names the red flags:
  "unexplained deductions finance teams cannot trace." They diagnosed the
  disease and shipped the symptom relief.
- **Dispute window:** most acquiring agreements allow only 60–180 days to
  contest a fee. Overcharges found later are written off permanently.

### 1.6 Honest competitive position

Fee validation is an established discipline. Optimus sells merchant fee
validation to enterprises. Cybiqon does it for Amazon/Flipkart sellers. Terra
Insight publishes an eight-pattern leakage taxonomy.

**Do not claim nobody has thought of this.** Claim what is actually true:

1. Existing tools are enterprise procurement products, not something the
   12 million merchants on Razorpay can use
2. They are marketplace-focused; gateway-side fee validation is thin
3. **None are agentic** — they are rules engines with dashboards
4. **Nobody has merged fee leakage with the new TDS regime** (new this year)

---

## 2. THE CORE ARCHITECTURAL RULE

> ## The LLM must never do arithmetic.

If you ask Claude "is ₹610 the right fee on a ₹25,000 card payment?", you get an
answer that is usually right and occasionally, silently, wrong. That destroys the
entire product, because the product IS accuracy.

**Split the system in two:**

| Layer | Implemented as | Job |
|---|---|---|
| **Calculator** | Plain Python, no LLM | Compute what the fee *should* be. Deterministic, unit-tested, never wrong. |
| **Judge** | Claude agent | Look at each gap and decide *what kind* of gap it is, and what to do about it. |

A calculator can tell you the fee was ₹610 instead of ₹412. It cannot tell you
whether that is a pricing error, a mislabelled instrument, an unclaimed refund,
or rounding. **That second question is judgment, and it is where the agent earns
its place.**

**If a judge asks "why not just write a script?":** the script finds the gaps,
the agent explains them. Finding is easy. Explaining is the product.

---

## 3. This is agentic AI, NOT machine learning

**There is no training. No dataset. No model weights. This is deliberate.**

| | ML approach (rejected) | Agentic approach (ours) |
|---|---|---|
| How it knows the rules | Learned from labelled examples | Told, in the prompt, every time |
| Output | `overcharge: 0.87` | "Overcharge of ₹64. UPI carries zero network MDR under the PSS Act..." |
| Updating a rule | Retrain on new data | Edit one line of JSON |
| Explainable? | No | Yes, natively |

Four reasons training would be wrong here:

1. **The rules are already written down.** UPI MDR = 0% is in the law. Training
   a model to learn `fee = amount * 0.004` is like training a neural network to
   learn the 7-times table.
2. **The training data does not exist.** You would need thousands of real
   settlement files with human-labelled overcharges. If that existed the problem
   would be solved.
3. **A trained model cannot defend itself.** Our entire pitch is explainability.
   No finance team can file a dispute with a confidence score.
4. **Rules change — and one just did.** On 17 Aug 2026 the authority under
   rule 1 was rewritten (§15.1). Fixing it took one edit to one JSON string.
   A trained model would need recollection, retraining and revalidation — and
   would be wrong the whole time. This is no longer a hypothetical argument;
   demo it.

---

## 4. Architecture

```
Razorpay test-mode API ──┐
                         ├──► [1] INGEST ──► SQLite
Synthetic generator ─────┘         orders, payments, refunds,
   (with planted errors)           settlements, bank credits
                                            │
                                            ▼
                              [2] EXPECTED-VALUE ENGINE   (pure Python)
                                  rate card → expected fee
                                  18% → expected GST
                                  refund + TDS rules
                                            │
                                            ▼
                              [3] VARIANCE DETECTOR
                                  actual − expected, per line
                                            │
                                            ▼
                              [4] CLAUDE AGENT   ◄── tools:
                                  classify + explain        rate_card_lookup()
                                  confidence + action       payment_detail()
                                            │               refund_history()
                                            ▼               tds_code_map()
                              [5] GUARDRAIL GATE            similar_past_cases()
                                  low confidence OR big ₹ → human queue
                                            │
                                            ▼
                              [6] REPORT
                                  match rate + exceptions grouped by ACTION
```

Six components. Build them in this order.

---

## 5. Exception taxonomy

**Categories are defined by what the merchant must DO, not by what the error
looks like.** This is the single most important design decision in the project.

| Code | What happened | Action | Recoverable? |
|---|---|---|---|
| `CLEAN` | Matches expected within tolerance | None | — |
| `ROUNDING` | Gap ≤ tolerance floor | Auto-dismiss | No |
| `ZERO_MDR_VIOLATION` | Network MDR charged on UPI/RuPay | **Dispute** | YES |
| `INSTRUMENT_MISLABEL` | UPI payment tagged as card, picked up card rate | **Dispute** | YES |
| `RATE_MISMATCH` | Charged above contracted/regulated slab | **Dispute** | YES |
| `GST_MISMATCH` | GST ≠ 18% of fee, or computed on wrong base | Fix books or dispute | Partly |
| `REFUND_MDR_RETAINED` | Fee kept on a refunded order | Book as cost | No — this is expected behaviour |
| `PERIOD_BOUNDARY` | Order in month N, settled in month N+1 | Reclassify, do not alarm | No |
| `TDS_CODE_MISMATCH` | Old section code used post-1-Apr-2026 | **Correct before filing** | Tax credit at risk |
| `MISSING_FROM_SETTLEMENT` | Order exists in books, no settlement line | **Chase urgently** | YES |
| `UNEXPLAINED` | Fits nothing above | Escalate to human | Unknown |

**Three of these mean "do nothing."** That is deliberate. A tool that flags
everything is useless. Knowing when NOT to alarm someone is the differentiator.

---

## 6. The rules

Budget: **10 rules.** Each rule is three pieces of work — the rule, a planted
test case, and verification that the rule is correct. A wrong rule is worse than
a missing rule: it makes the agent confidently accuse Razorpay of overcharging
on every transaction.

**Only add a rule when the correct answer is written down by someone else.**

| # | Rule | Source |
|---|---|---|
| 1 | UPI (bank account) network MDR = 0% | PSS Act s.10A, as amended 17 Aug 2026 (see §15.1) |
| 2 | RuPay debit network MDR = 0% | Same |
| 3 | Visa/MC debit, ticket ≤ ₹2,000 → cap 0.40% | RBI circular RBI/2017-18/105 |
| 4 | Visa/MC debit, ticket > ₹2,000 → cap 0.90% | Same |
| 5 | Credit card → contracted slab (typ. 1.4–2.5% domestic) | Merchant rate card |
| 6 | Amex / Diners / international / corporate → premium slab (~3%) | Merchant rate card |
| 7 | GST = 18% **of the fee**, never of transaction value | GST law |
| 8 | Refund → original fee is retained (expected, NOT an error) | Industry standard, all Indian gateways |
| 9 | Instrument mislabel: `method` says card but a UPI reference (RRN/UMN) is present | Cross-field consistency check |
| 10 | TDS code must match its date: 194O before 1 Apr 2026, code 1035 under §393(1) Sl. 8(v) after | Income Tax Act 2025 |

**Rule 8 is the sleeper.** It prevents an entire class of false alarms, which
matters more for the accuracy number than catching one extra overcharge.

### 6.1 Leave these to the agent — do NOT write rules for them

| Situation | Why not a rule |
|---|---|
| Two explanations both fit a ₹198 gap | Requires weighing evidence, not a lookup |
| Order in May, settled in June | Depends on merchant's accounting period |
| An adjustment line never seen before | Cannot write a rule for the unknown |
| Dispute this or absorb it? | Depends on amount, relationship, effort |
| Writing the dispute paragraph | Language, not logic |

Say this explicitly in the pitch: *"We hard-coded ten rules where the law is
unambiguous, and deliberately left judgment to the agent where it isn't."*

### 6.2 The tolerance band — most important single number

> **How big must a gap be before it counts as an exception?**

Too tight → flags rounding noise, drowns the merchant, looks unreliable.
Too loose → misses real overcharges.

**Starting value: ₹1 or 0.5%, whichever is larger.**
The ₹1 floor handles rounding; the percentage scales for large transactions.

**Put this in config, never hard-code it.** Mention on stage that it is tunable —
someone will ask.

---

## 7. Ground truth: the trick that makes the demo work

**The synthetic data generator plants known errors and returns the answer key.**

```python
def generate_batch(n=60) -> tuple[list[Record], dict[str, str]]:
    """
    Returns (records, ground_truth) where ground_truth maps
    record_id -> expected exception code.

    Composition for n=60:
      48 x CLEAN
       3 x ZERO_MDR_VIOLATION
       2 x INSTRUMENT_MISLABEL
       2 x RATE_MISMATCH
       2 x GST_MISMATCH
       1 x MISSING_FROM_SETTLEMENT
       1 x TDS_CODE_MISMATCH
       1 x PERIOD_BOUNDARY

    Plus decoys that must NOT be flagged:
       sub-rupee rounding differences (expect ROUNDING)
       refunded orders with retained fees (expect REFUND_MDR_RETAINED)
    """
```

This gives a **measured** claim on stage:

> "We planted 12 anomalies in 60 records. The agent found 11, correctly dismissed
> 6 decoys, escalated 1 it wasn't sure about. 91.7% recall, zero false accusations."

**Build this on day one.** It is simultaneously the test harness, the demo data,
and the scoreboard.

### 7.1 Real vs synthetic data — be honest

Razorpay test mode will not produce 60 realistic settlements containing planted
errors. Do not waste days trying.

**Hybrid approach:**
- Use Razorpay test mode for **schema fidelity** — create real orders, payments
  and refunds so field names, ID formats and structures are genuine
- Generate **volume and planted errors** synthetically in that exact shape

State this plainly in the README. Judges respect an honest data strategy far
more than a vague one.

---

## 8. Stack

Solo builder, ~11 days, weaker on engineering. **Optimise for fewest moving parts.**

| Layer | Choice | Why |
|---|---|---|
| Language | Python | Best fit for the rate/tax logic |
| Storage | **SQLite** | One file. No server, no Docker, no ops. |
| Agent | **Claude Agent SDK** | Handles the tool-call loop; also what Razorpay's own Agent Studio is built on — say so in the pitch |
| API | FastAPI | Minimal |
| UI | React single page | Build last |
| Charts | Recharts | Simple |

**Explicitly skip:** Postgres, Docker, auth, Redis, queues, cloud deployment,
tests beyond the ground-truth harness. Each is a day that does not exist.

---

## 9. Data model

Mirror Razorpay's actual settlement recon API field names so the schema is real.

```sql
-- from Razorpay Orders/Payments API
payments (
  payment_id TEXT PRIMARY KEY,   -- pay_XXXXXXXX
  order_id TEXT,                 -- order_XXXXXXXX
  amount INTEGER,                -- paise, NOT rupees
  currency TEXT,
  method TEXT,                   -- upi | card | netbanking | wallet | emi
  card_network TEXT,             -- visa | mastercard | rupay | amex | diners
  card_type TEXT,                -- debit | credit
  is_international BOOLEAN,
  upi_reference TEXT,            -- RRN/UMN when present; used by rule 9
  created_at INTEGER             -- unix ts
)

-- from Razorpay Settlement Recon API
settlement_lines (
  entity_id TEXT PRIMARY KEY,
  settlement_id TEXT,            -- setl_XXXXXXXX
  type TEXT,                     -- payment | refund | transfer | adjustment
  payment_id TEXT,
  order_id TEXT,
  amount INTEGER,                -- paise
  fee INTEGER,                   -- paise, what Razorpay ACTUALLY charged
  tax INTEGER,                   -- paise, GST on fee
  utr TEXT,
  settled_at INTEGER
)

bank_credits (
  utr TEXT PRIMARY KEY,
  amount INTEGER,
  credited_at INTEGER
)

rate_card (                      -- the merchant's contract
  instrument TEXT PRIMARY KEY,   -- upi | rupay_debit | visa_debit_low | ...
  rate_bps INTEGER,              -- basis points
  cap_bps INTEGER,
  source TEXT                    -- 'RBI/2017-18/105' etc, for provenance
)

variances (
  id INTEGER PRIMARY KEY,
  payment_id TEXT,
  expected_fee INTEGER,
  actual_fee INTEGER,
  expected_tax INTEGER,
  actual_tax INTEGER,
  delta INTEGER,                 -- paise
  exception_code TEXT,           -- from taxonomy in section 5
  confidence REAL,               -- 0.0 - 1.0
  reasoning TEXT,                -- agent's explanation
  rule_cited TEXT,               -- which rule fired + its source
  action TEXT,                   -- dismiss | dispute | fix_books | escalate
  dispute_text TEXT,             -- paste-ready paragraph
  human_reviewed BOOLEAN DEFAULT 0,
  created_at INTEGER
)
```

**ALL MONEY IN PAISE, AS INTEGERS.** Never use floats for currency. Convert to
rupees only at display time.

---

## 10. Guardrails (non-negotiable)

These are part of the pitch, not just hygiene. State them on stage.

1. **The agent never writes to a ledger.** It proposes; a human disposes.
2. **Every classification carries** a reasoning trace, a confidence score, and
   the rule it relied on with that rule's source.
3. **Escalate, don't guess:** confidence below threshold OR delta above a rupee
   cap → human review queue, never auto-resolved.
4. **The agent never invents a balancing entry.** If something doesn't reconcile,
   it says so. A "plug entry" to make books look tidy is the exact audit finding
   nobody wants.
5. **Full audit log** — every agent decision, timestamped and replayable.

---

## 11. Build order

Checkpoints, not a calendar. Move to the next only when the current one is done.

| # | Build | Done when |
|---|---|---|
| 1 | Razorpay test mode: create orders/payments/refunds; pull settlement recon. SQLite schema. | Real data lands in the DB |
| 2 | Synthetic generator with planted errors + ground truth | `generate_batch(60)` returns data AND answer key |
| 3 | Expected-value engine: rate card, rules 1–8 | Given any payment, outputs correct expected fee + GST |
| 4 | Unit tests for the engine | Every rule has a passing test with a citable source |
| 5 | Variance detector + taxonomy enum | Every record gets a raw delta |
| 6 | Claude agent: tools, classification, confidence, explanation | Agent labels all 60 records |
| 7 | Guardrail gate + scoring vs ground truth | A real accuracy percentage exists |
| 8 | Dispute-text generation | Output is paste-ready into a support ticket |
| 9 | React dashboard: run batch → match rate → exception table → click for reasoning | Demo-able end to end |
| 10 | README, rehearse the 5-min demo aloud | Can run it without touching a keyboard |

**Hard cutoff:** if the agent is not classifying by checkpoint 6, drop the React
UI and demo through clean terminal output. A working system in a terminal beats
a pretty UI around a broken one. Judges know the difference.

**If ahead of schedule**, in priority order:
1. Better dispute text (paste-ready, no editing needed)
2. Confidence calibration
3. Rule provenance in every output — citing "RBI circular RBI/2017-18/105" is
   far more persuasive than a bare number
4. **Resolution memory** (see §12)

---

## 12. Resolution memory (optional, only if ahead)

Not training — but it looks and feels like the system learning.

Store how past exceptions were resolved. When a similar variance appears,
retrieve 2–3 past cases and include them in the agent's prompt as examples.

**Example:** merchant previously confirmed a recurring adjustment line is their
monthly AMC. Next month the agent sees it, recalls the resolution, and dismisses
it instead of flagging it again.

Half a day's work. Just a database query plus text in a prompt.

---

## 13. Demo script (5 minutes)

| Time | Beat |
|---|---|
| 0:10 | "Meera got ₹7,370 instead of ₹9,000. Razorpay tells her what they charged. Nobody tells her what they *should* have charged." |
| 0:40 | Show a real settlement report. Point at one line. "Is this right? You cannot tell. Neither can she." |
| 1:40 | Run the batch live. 60 records. |
| 3:10 | Results: 48 clean, 9 recoverable overcharges totalling ₹X, 2 tax-credit risks, 1 escalated. Click one — show reasoning trace and ready-to-send dispute text. |
| 4:10 | **Accuracy against ground truth.** This is the moment. |
| 4:40 | "The agent never touches a ledger. It proposes, a human approves. Every decision is logged." |

---

## 14. Glossary (Indian payments/tax terms)

| Term | Meaning |
|---|---|
| **MDR** | Merchant Discount Rate — the % fee deducted per transaction. Also called TDR. |
| **UPI** | India's real-time payment rail. **Network MDR 0% since 1 Jan 2020** — still 0%, but since 17 Aug 2026 that rests on a Central Government notification rather than on statute. See §15.1. |
| **RuPay** | India's domestic card network. RuPay debit also 0% network MDR. |
| **Platform fee** | What a gateway charges *on top of* network MDR. **This is legal on zero-MDR rails** — the leakage is when it's mislabelled as MDR, or a flat card-grade rate is applied to UPI volume. |
| **Settlement** | Batch transfer of collected money to the merchant's bank, net of deductions. Razorpay standard is T+2 working days. |
| **UTR** | Unique Transaction Reference — the bank's ID for a settlement credit. Join key between settlement report and bank statement. |
| **GST** | India's consumption tax. **18% on gateway fees**, claimable as input tax credit. |
| **ITC** | Input Tax Credit — GST paid on business inputs, recoverable against GST owed. |
| **TDS** | Tax Deducted at Source — tax withheld by the payer before paying you. |
| **Section 194O** | Old provision: e-commerce operators deduct TDS on seller payments. **Ceased to exist 1 Apr 2026.** |
| **Section 393(1) Sl. 8(v) / code 1035** | Its replacement under the Income Tax Act 2025. Rate cut from 1% to 0.1%. |
| **Form 26AS** | Old annual tax credit statement. |
| **Form 168** | Its replacement from FY 2026-27. Uses 4-digit payment codes (1001–1092) instead of section names. |
| **Chargeback** | Customer disputes a card payment; the amount plus fees is debited from the merchant. |
| **Interchange** | The share of MDR that goes to the card-issuing bank (70–80%). |

---

## 15. Key context: the tax regime change

**On 1 April 2026 India replaced its entire income tax law.** The Income Tax Act
2025 replaced the 1961 Act. Over fifty separate TDS sections (194C, 194J, 194O,
etc.) collapsed into one umbrella provision, Section 393.

Critically: **the government changed the join key.** TDS returns and Form 168
identify payments by a four-digit code (1001–1092) instead of a section name.
That is a reconciliation problem by definition.

The transition artefact this creates:

| Sale settled | Deducted under | Appears as | In |
|---|---|---|---|
| March 2026 | Section 194O | "194O", 1% | Form 26AS |
| April 2026 | Section 393(1) Sl. 8(v) | Code **1035**, 0.1% | Form **168** |

Same platform, same seller, same PAN. Two laws, two rates, two identifier
systems, two documents. Quoting an old section code on a post-April-2026 return
triggers validation rejection and a ₹200/day late fee — and the seller's tax
credit may not appear at all.

FY 2026-27 is the first full year under the new Act. **Nobody's tooling has
caught up.** This is rule 10, and it is time-boxed urgency for the pitch.

---

### 15.1 The MDR authority changed on 17 August 2026

**This is rule 1 and rule 2, and it moved after those rules were written.**

PSS Act s.10A used to protect payment modes *prescribed under s.269SU of the
Income-tax Act, 1961*. The Taxation and Other Laws (Amendment) Act 2026, and a
companion Act amending the PSS Act, received Presidential assent on
**17 August 2026**. They **cut the s.269SU link**. The modes that carry zero
MDR are now whichever ones **the Central Government notifies**.

| | Before | After 17 Aug 2026 |
|---|---|---|
| Authority | PSS Act s.10A read with IT Act s.269SU | PSS Act s.10A alone |
| What is protected | Modes prescribed under s.269SU | Modes the Centre notifies |
| Can it change? | Needs an amendment | Needs a notification |

**The rate has not changed.** UPI and RuPay debit are still zero-MDR. Nothing
the engine computes today is wrong.

**What changed is the nature of the rule.** It used to be hard statute. It is
now conditional on a notification that can be varied without Parliament.
Proposals under discussion: roughly 0.25–0.5% on UPI above ₹2,000, for
merchants above a turnover threshold. If that is notified, rule 1 stops being
universal and needs a merchant-turnover input the engine does not currently
take.

**Use this on stage.** §3 argues rules beat training because rules change and a
config edit takes two minutes. This is that argument with a date on it, six days
before the pitch. Show the old citation, show the one-line edit, re-run the
batch, show identical findings with corrected provenance.

---

## 16. Things NOT to do

- **Do not let the LLM compute fees.** Ever. See §2.
- **Do not train a model.** See §3.
- **Do not use floats for money.** Paise, as integers.
- **Do not add rules beyond the ten** unless the source is citable and you have
  time to verify. A wrong rule is worse than a missing rule.
- **Do not claim nobody has thought of fee validation.** See §1.6.
- **Do not let the agent write to a ledger or invent balancing entries.**
- **Do not flag everything.** Three exception codes mean "do nothing" — those
  matter as much as the ones that mean "dispute."
- **Do not build auth, Docker, Postgres, or cloud deployment.**
- **Do not spend more than one checkpoint fighting Razorpay test mode.** Fall
  back to the synthetic generator; be honest about it in the README.
