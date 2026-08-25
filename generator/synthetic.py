"""
Synthetic settlement generator with planted errors and a ground-truth answer key.

This file is the test harness, the demo data and the scoreboard all at once.
See CLAUDE.md section 7.

Two things make it worth building before the detector or the agent:

  1. Nothing downstream can be measured without an answer key. "The agent found
     some overcharges" is not a claim. "The agent found 14 of 14 planted
     anomalies and falsely accused zero clean records" is.

  2. Razorpay test mode will not hand us 60 settlements containing known
     overcharges. It gives us the SHAPE (field names, ID formats). We supply
     the volume and the errors. CLAUDE.md section 7.1.

ALL MONEY IS INTEGER PAISE. Never floats.

THE ANSWER KEY NEVER TOUCHES THE DATA. The Record objects carry no label
field of any kind - the planted code exists only in the separate dict that
generate_batch returns. If the label lived on the record, a careless line in
the detector or a stray field in an agent prompt could read it, and the
accuracy number would silently become a lie.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from engine.expected_value import (
    Payment,
    compute_expected_fee,
    load_rate_card,
    rupees,
    tolerance_paise,
)
# The generator must round money exactly the way the engine does, or a "clean"
# record could differ from expectation by a paise purely from arithmetic drift.
# Importing the engine's own helper is deliberate - one rounding rule, one place.
from engine.expected_value import _bps as apply_bps


# --- ground-truth composition -------------------------------------------
#
# The canonical mix at n=60. CLAUDE.md section 7 lists 48 records that are not
# anomalies; six of those are DECOYS - records that look wrong to a naive
# checker and must NOT be flagged.
#
# Two anomalies were added beyond CLAUDE.md's list, taken out of the CLEAN
# allocation. They exist because a measurement showed the original twelve were
# not testing what we claimed: every record reaching the agent carried exactly
# ONE candidate explanation, so the agent was confirming the detector rather
# than judging anything. A hundred percent on that batch measures the ten rules.
# See RECIPE_TRUTH below for what the two new ones do.
#
# Anomalies:  3 + 2 + 2 + 2 + 1 + 1 + 1 + 1 + 1 = 14
# Decoys:     3 + 3                             =  6   <- correctly dismissed
# Clean:                                          40
#                                                 --
#                                                 60
CANONICAL_N = 60
CANONICAL_MIX: dict[str, int] = {
    # nothing wrong
    "CLEAN": 40,
    # decoys - a gap exists, or looks like it does, but the answer is "do nothing"
    "ROUNDING": 3,
    "REFUND_MDR_RETAINED": 3,
    # real anomalies
    "ZERO_MDR_VIOLATION": 3,
    "INSTRUMENT_MISLABEL": 2,
    "RATE_MISMATCH": 2,
    "GST_MISMATCH": 2,
    "MISSING_FROM_SETTLEMENT": 1,
    "TDS_CODE_MISMATCH": 1,
    "PERIOD_BOUNDARY": 1,
    # the two that actually test the agent rather than the rules
    "AMBIGUOUS_REFUND_OVERCHARGE": 1,
    "UNRECOGNISED_ADJUSTMENT": 1,
}

# Most recipes are named after the answer they expect. These two are not, and
# that distinction is the point of them.
#
# Everything above plants an error that exactly one of the ten rules explains,
# which means the detector proposes one candidate and the agent confirms it. A
# batch made only of those measures the rules, not the judgement. These two
# break that:
#
#   AMBIGUOUS_REFUND_OVERCHARGE  a refunded order that was ALSO overcharged.
#       Two signals fire and they point different ways. Rule 8 says a retained
#       fee on a refund is expected and should be dismissed; the zero-MDR rule
#       says this particular fee was never chargeable. The second reading is
#       correct - otherwise the cheapest way to hide an overcharge would be to
#       refund the order. The agent has to take the harder reading.
#
#   UNRECOGNISED_ADJUSTMENT      a deduction that matches no rule at all.
#       CLAUDE.md section 6.1: "an adjustment line never seen before - cannot
#       write a rule for the unknown." The only correct answer is that it
#       cannot be accounted for. This is the one case where inventing a
#       plausible explanation is the failure, and saying "I don't know" is the
#       pass.
RECIPE_TRUTH: dict[str, str] = {
    "AMBIGUOUS_REFUND_OVERCHARGE": "ZERO_MDR_VIOLATION",
    "UNRECOGNISED_ADJUSTMENT": "UNEXPLAINED",
}


def truth_for(recipe: str) -> str:
    """The exception code a recipe should produce. Identity unless mapped."""
    return RECIPE_TRUTH.get(recipe, recipe)


def canonical_truth_mix() -> dict[str, int]:
    """CANONICAL_MIX restated in terms of expected answers rather than recipes."""
    out: dict[str, int] = {}
    for recipe, n in CANONICAL_MIX.items():
        out[truth_for(recipe)] = out.get(truth_for(recipe), 0) + n
    return out

RECOVERABLE_CODES = {
    "ZERO_MDR_VIOLATION",
    "INSTRUMENT_MISLABEL",
    "RATE_MISMATCH",
    "MISSING_FROM_SETTLEMENT",
}
DECOY_CODES = {"ROUNDING", "REFUND_MDR_RETAINED"}


# --- the settlement period ----------------------------------------------
#
# June 2026. Two months into the new Income Tax Act, so TDS code 1035 is the
# correct one and 194O is the anachronism rule 10 must catch.
PERIOD_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 6, 26, tzinfo=timezone.utc)
MONTH_END = datetime(2026, 6, 30, 21, 40, tzinfo=timezone.utc)  # the boundary case

TDS_NEW_CODE = "1035"      # s.393(1) Sl. 8(v), Income Tax Act 2025
TDS_NEW_BPS = 10           # 0.1%
TDS_OLD_CODE = "194O"      # ceased 2026-04-01
TDS_OLD_BPS = 100          # 1%
TDS_REGIME_CHANGE = datetime(2026, 4, 1, tzinfo=timezone.utc)


# --- record shapes -------------------------------------------------------
# These mirror the tables in CLAUDE.md section 9 one-for-one, so checkpoint 1's
# ingest can INSERT them without translation.

@dataclass
class SettlementLine:
    entity_id: str          # Razorpay recon uses the pay_/rfnd_ id itself
    settlement_id: str      # setl_XXXXXXXXXXXXXX
    type: str               # payment | refund | adjustment
    payment_id: str
    order_id: str
    amount: int             # paise; negative for refund rows
    fee: int                # paise - what the gateway ACTUALLY charged
    tax: int                # paise - GST the gateway ACTUALLY charged
    utr: str
    settled_at: int         # unix ts


@dataclass
class Refund:
    refund_id: str          # rfnd_XXXXXXXXXXXXXX
    payment_id: str
    amount: int             # paise, positive number, money going back out
    created_at: int


@dataclass
class TdsEntry:
    """
    Not in CLAUDE.md section 9's schema - rule 10 needs somewhere to live.

    Modelled as a separate table rather than netted into the settlement.
    Real e-commerce TDS is withheld from the payout; we keep it out of the
    settlement arithmetic here so that the fee/GST variance stays readable and
    the bank credit still ties out to the paise. Stated plainly so nobody
    mistakes the simplification for an oversight.
    """
    payment_id: str
    section_code: str       # "1035" (correct post-Apr-2026) or "194O" (stale)
    rate_bps: int
    amount: int             # paise withheld
    deducted_at: int


@dataclass
class BankCredit:
    utr: str
    amount: int             # paise actually landing in the bank account
    credited_at: int


@dataclass
class Record:
    """
    One order's worth of everything, joined. No label field - see module docstring.
    """
    record_id: str          # == payment_id; the join key for the answer key
    order_id: str
    payment: Payment        # the engine's own dataclass, so it can be fed straight in
    created_at: int
    settlement_lines: list[SettlementLine] = field(default_factory=list)
    refund: Optional[Refund] = None
    tds: Optional[TdsEntry] = None


@dataclass
class Batch:
    """
    CLAUDE.md's sketch returns just (records, ground_truth). Bank credits have
    nowhere to live in that shape - a single credit spans many records - and
    Layer 1 ("did the money arrive?") needs them. So the batch is an object.
    """
    records: list[Record]
    bank_credits: list[BankCredit]
    seed: int
    rate_card: dict

    @property
    def settlement_ids(self) -> list[str]:
        return sorted({ln.settlement_id for r in self.records for ln in r.settlement_lines})


# --- id and date plumbing ------------------------------------------------

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rzp_id(rng: random.Random, prefix: str) -> str:
    """Razorpay ids are a prefix plus 14 base62 characters."""
    return prefix + "".join(rng.choice(_ID_ALPHABET) for _ in range(14))


def _utr(rng: random.Random) -> str:
    """Bank-style UTR, as it appears on the statement: HDFCN + 8 digits."""
    return "HDFCN" + "".join(rng.choice("0123456789") for _ in range(8))


def _add_working_days(dt: datetime, days: int) -> datetime:
    """Razorpay settles T+2 working days. Weekends do not count."""
    out = dt
    remaining = days
    while remaining > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            remaining -= 1
    return out


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


# --- what the merchant actually sells ------------------------------------
#
# A boutique's real instrument mix, weighted. UPI dominates, which is exactly
# why zero-MDR violations are the anomaly worth catching.

_INSTRUMENT_MIX: list[tuple[int, str]] = [
    (40, "upi"),
    (18, "credit_card"),
    (10, "debit_card_low"),
    (8, "debit_card_high"),
    (8, "rupay_debit"),
    (6, "netbanking"),
    (5, "wallet"),
    (3, "premium_card"),
    (2, "international"),
]

# Realistic ticket bands per instrument, in paise.
_AMOUNT_BANDS: dict[str, tuple[int, int]] = {
    "upi": (20_000, 500_000),
    "credit_card": (100_000, 2_500_000),
    "debit_card_low": (15_000, 200_000),      # must stay <= Rs 2,000
    "debit_card_high": (200_100, 900_000),    # must stay >  Rs 2,000
    "rupay_debit": (20_000, 600_000),
    "netbanking": (150_000, 1_200_000),
    "wallet": (10_000, 150_000),
    "premium_card": (300_000, 3_000_000),
    "international": (200_000, 2_000_000),
}


def _pick_instrument(rng: random.Random) -> str:
    total = sum(w for w, _ in _INSTRUMENT_MIX)
    roll = rng.randrange(total)
    for weight, key in _INSTRUMENT_MIX:
        roll -= weight
        if roll < 0:
            return key
    return "upi"


def _amount_for(rng: random.Random, instrument: str) -> int:
    low, high = _AMOUNT_BANDS[instrument]
    # Round to whole rupees. Real card terminals rarely produce stray paise, and
    # it keeps the planted rounding decoys unambiguous.
    return rng.randrange(low // 100, high // 100 + 1) * 100


def _payment_fields(instrument: str) -> dict:
    """Turn a rate-card key back into the Razorpay fields that imply it."""
    return {
        "upi": dict(method="upi"),
        "rupay_debit": dict(method="card", card_network="rupay", card_type="debit"),
        "debit_card_low": dict(method="card", card_network="visa", card_type="debit"),
        "debit_card_high": dict(method="card", card_network="mastercard", card_type="debit"),
        "credit_card": dict(method="card", card_network="visa", card_type="credit"),
        "premium_card": dict(method="card", card_network="amex", card_type="credit"),
        "international": dict(method="card", card_network="visa", card_type="credit",
                              is_international=True),
        "netbanking": dict(method="netbanking"),
        "wallet": dict(method="wallet"),
    }[instrument]


# --- composition scaling -------------------------------------------------

def scale_mix(n: int) -> dict[str, int]:
    """
    Scale the canonical 60-record mix to any batch size.

    Every anomaly type keeps at least one representative however small the
    batch - a batch with no TDS_CODE_MISMATCH in it cannot tell you whether
    rule 10 works. CLEAN absorbs the remainder.
    """
    if n < 12:
        raise ValueError(
            f"n={n} is too small: {len(CANONICAL_MIX) - 1} non-clean types need "
            "at least one record each, plus some clean records to measure "
            "false accusations against."
        )

    counts: dict[str, int] = {}
    for code, canonical in CANONICAL_MIX.items():
        if code == "CLEAN":
            continue
        counts[code] = max(1, round(canonical * n / CANONICAL_N))

    clean = n - sum(counts.values())
    if clean < 1:
        raise ValueError(f"n={n} leaves no room for clean records")
    counts["CLEAN"] = clean
    return counts


# --- planting ------------------------------------------------------------
#
# One builder per exception code. Each returns (payment_fields, actual_fee,
# actual_tax, extras). Keeping them separate means a planted error is one
# readable function, and adding an eleventh is not a refactor.

def _plant(rng: random.Random, code: str, rate_card: dict) -> dict:
    """
    Build the raw ingredients of one record for a given planted code.

    Returns a dict with: instrument, amount, fields, fee, tax, and optional
    make_refund / tds_code / created_at / settled_offset overrides.
    """
    # --- pick an instrument the anomaly can actually live on -------------
    if code == "ZERO_MDR_VIOLATION":
        instrument = rng.choice(["upi", "rupay_debit"])
    elif code == "INSTRUMENT_MISLABEL":
        instrument = "credit_card"
    elif code == "RATE_MISMATCH":
        instrument = rng.choice(["credit_card", "premium_card"])
    elif code == "REFUND_MDR_RETAINED":
        instrument = rng.choice(["upi", "credit_card", "debit_card_high"])
    elif code == "AMBIGUOUS_REFUND_OVERCHARGE":
        instrument = rng.choice(["upi", "rupay_debit"])
    elif code == "UNRECOGNISED_ADJUSTMENT":
        instrument = _pick_instrument(rng)
    else:
        instrument = _pick_instrument(rng)

    amount = _amount_for(rng, instrument)
    fields = _payment_fields(instrument)

    # Rule 9's signature: the payment says "card" but carries a UPI reference.
    if code == "INSTRUMENT_MISLABEL":
        fields = dict(fields, upi_reference="".join(rng.choice("0123456789") for _ in range(12)))

    # --- what the rules say it SHOULD have cost --------------------------
    probe = Payment(payment_id="probe", amount=amount, **fields)
    expected = compute_expected_fee(probe, rate_card)
    fee, tax = expected.total_fee_paise, expected.gst_paise

    out = dict(instrument=instrument, amount=amount, fields=fields,
               fee=fee, tax=tax, make_refund=False, tds_code=None,
               created_at=None, adjustment=None)

    if code in ("CLEAN", "PERIOD_BOUNDARY", "TDS_CODE_MISMATCH"):
        pass  # charged exactly right; the problem, if any, is elsewhere

    elif code == "ROUNDING":
        # A gap that exists but sits under the tolerance band. The correct
        # answer is "auto-dismiss" - flagging this is what makes a tool
        # unusable, so it has to be in the batch.
        tol = tolerance_paise(fee, rate_card)
        drift = rng.randint(1, max(1, tol - 1)) * rng.choice([-1, 1])
        fee = max(0, fee + drift)
        tax = apply_bps(fee, rate_card["gst_rate_bps"])

    elif code == "REFUND_MDR_RETAINED":
        # Fee is CORRECT. The order was refunded and the gateway kept the fee -
        # which is expected behaviour at every Indian gateway. Rule 8. The
        # record looks alarming and must not be treated as one.
        out["make_refund"] = True

    elif code == "ZERO_MDR_VIOLATION":
        # A network MDR component charged on a rail where it is mandated to
        # zero. 0.90% - a debit-card rate applied to UPI volume.
        fee = fee + apply_bps(amount, 90)
        tax = apply_bps(fee, rate_card["gst_rate_bps"])

    elif code == "INSTRUMENT_MISLABEL":
        # NOTE, and this is the subtle one: the fee charged is exactly the
        # correct CREDIT CARD fee, so the arithmetic delta is ZERO. Nothing in
        # a fee comparison can see this. It is caught only by rule 9's
        # cross-field note - a card payment carrying a UPI reference. The
        # recoverable amount is card-rate minus UPI-rate, which checkpoint 5
        # must compute with a "what if this were UPI" re-run of the engine.
        pass

    elif code == "RATE_MISMATCH":
        # Charged above the contracted slab: 0.40% over, e.g. 2.40% on a 2.00%
        # credit-card contract. Small enough to look plausible on one line and
        # material across a month.
        spec = rate_card["instruments"][instrument]
        contracted = spec["network_mdr_bps"] + spec["platform_fee_bps"]
        fee = apply_bps(amount, contracted + 40)
        tax = apply_bps(fee, rate_card["gst_rate_bps"])

    elif code == "GST_MISMATCH":
        # Fee correct, GST wrong. Two flavours:
        #   base  - 18% charged on the TRANSACTION VALUE instead of on the fee
        #   rate  - 12% charged on the fee instead of 18%
        #
        # The "rate" flavour is only planted when it is actually FINDABLE. On a
        # Rs 300 UPI sale the fee is Rs 1.20, so the difference between 12% and
        # 18% of it is eight paise - far under the Rs 1 tolerance floor. Planting
        # that would be planting an anomaly no honest detector could ever catch,
        # and it would show up on stage as the agent "missing" one. Anything we
        # count in the denominator has to be detectable in principle.
        wrong_rate = apply_bps(fee, 1200)
        findable = abs(tax - wrong_rate) > tolerance_paise(tax, rate_card)
        if findable and rng.random() < 0.5:
            tax = wrong_rate
        else:
            tax = apply_bps(amount, rate_card["gst_rate_bps"])

    elif code == "MISSING_FROM_SETTLEMENT":
        # Fee/tax are what they WOULD have been. No settlement line is written
        # at all - the record exists in the merchant's books and nowhere else.
        pass

    elif code == "AMBIGUOUS_REFUND_OVERCHARGE":
        # Both at once: the order was refunded AND the fee was never chargeable.
        # Rule 8 will fire (a fee was retained on a refund - normally expected)
        # and so will rule 1/2 (network MDR on a zero-MDR rail - never allowed).
        # The refund is the comfortable reading and the wrong one.
        out["make_refund"] = True
        fee = fee + apply_bps(amount, 90)
        tax = apply_bps(fee, rate_card["gst_rate_bps"])

    elif code == "UNRECOGNISED_ADJUSTMENT":
        # The fee and GST on the payment are perfectly correct. What is wrong is
        # a separate deduction sitting in the same settlement with no
        # explanation attached to it - the "unexplained deduction finance teams
        # cannot trace" that Razorpay's own transparency blog names.
        #
        # Deliberately not a round number and not any multiple of the sale, so
        # that no arithmetic coincidence offers a false explanation.
        out["adjustment"] = rng.randrange(15_000, 90_000) + rng.randrange(1, 99)

    else:
        raise ValueError(f"no planting rule for code {code!r}")

    out["fee"], out["tax"] = fee, tax

    if code == "PERIOD_BOUNDARY":
        # Ordered on the last night of June, settled in July. Nothing is wrong;
        # it belongs to a different accounting period. Reclassify, do not alarm.
        out["created_at"] = MONTH_END

    if code == "TDS_CODE_MISMATCH":
        out["tds_code"] = TDS_OLD_CODE      # stale section, post-regime-change

    return out


# --- the generator -------------------------------------------------------

def generate_batch(n: int = 60, seed: int = 20260905,
                   rate_card: Optional[dict] = None) -> tuple[Batch, dict[str, str]]:
    """
    Returns (batch, ground_truth) where ground_truth maps record_id -> the
    exception code the pipeline is SUPPOSED to arrive at.

    Deterministic: the same seed gives byte-identical output. The demo must
    produce the same numbers on stage as it did in rehearsal.
    """
    rng = random.Random(seed)
    rc = rate_card if rate_card is not None else load_rate_card()

    counts = scale_mix(n)

    # Shuffle the plan BEFORE anything is built, so planted records are not
    # clustered and their ids do not encode their position in the plan.
    plan: list[str] = [code for code, k in counts.items() for _ in range(k)]
    rng.shuffle(plan)

    window = int((PERIOD_END - PERIOD_START).total_seconds())

    built: list[tuple[datetime, str, dict]] = []
    for code in plan:
        ingredients = _plant(rng, code, rc)
        created = ingredients["created_at"] or (
            PERIOD_START + timedelta(seconds=rng.randrange(window))
        )
        built.append((created, code, ingredients))

    # A settlement report arrives in time order.
    built.sort(key=lambda row: row[0])

    # One settlement batch per settlement date, with its own id and UTR - the
    # join key back to the bank statement.
    settlement_by_date: dict[str, tuple[str, str]] = {}

    records: list[Record] = []
    ground_truth: dict[str, str] = {}

    for created, code, ing in built:
        payment_id = _rzp_id(rng, "pay_")
        order_id = _rzp_id(rng, "order_")

        payment = Payment(payment_id=payment_id, amount=ing["amount"], **ing["fields"])
        settled = _add_working_days(created, 2)

        day = settled.strftime("%Y-%m-%d")
        if day not in settlement_by_date:
            settlement_by_date[day] = (_rzp_id(rng, "setl_"), _utr(rng))
        settlement_id, utr = settlement_by_date[day]

        record = Record(
            record_id=payment_id,
            order_id=order_id,
            payment=payment,
            created_at=_ts(created),
        )

        # --- settlement lines --------------------------------------------
        if code != "MISSING_FROM_SETTLEMENT":
            record.settlement_lines.append(SettlementLine(
                entity_id=payment_id,
                settlement_id=settlement_id,
                type="payment",
                payment_id=payment_id,
                order_id=order_id,
                amount=ing["amount"],
                fee=ing["fee"],
                tax=ing["tax"],
                utr=utr,
                settled_at=_ts(settled),
            ))

        # --- refund, for the rule-8 decoy --------------------------------
        if ing["make_refund"]:
            refund_id = _rzp_id(rng, "rfnd_")
            refunded_at = created + timedelta(hours=rng.randrange(6, 40))
            record.refund = Refund(
                refund_id=refund_id,
                payment_id=payment_id,
                amount=ing["amount"],
                created_at=_ts(refunded_at),
            )
            # The refund row carries NO fee reversal. That is the whole point:
            # the gateway keeps what it already charged.
            record.settlement_lines.append(SettlementLine(
                entity_id=refund_id,
                settlement_id=settlement_id,
                type="refund",
                payment_id=payment_id,
                order_id=order_id,
                amount=-ing["amount"],
                fee=0,
                tax=0,
                utr=utr,
                settled_at=_ts(settled),
            ))

        # --- an adjustment nobody can account for ---------------------------
        if ing["adjustment"]:
            record.settlement_lines.append(SettlementLine(
                entity_id=_rzp_id(rng, "adj_"),
                settlement_id=settlement_id,
                type="adjustment",
                payment_id=payment_id,
                order_id=order_id,
                amount=-ing["adjustment"],
                fee=0,
                tax=0,
                utr=utr,
                settled_at=_ts(settled),
            ))

        # --- TDS -----------------------------------------------------------
        # Withheld on roughly one record in eight, so rule 10 has correct
        # entries to leave alone as well as the one stale code to catch.
        if ing["tds_code"] is not None or rng.random() < 0.12:
            stale = ing["tds_code"] == TDS_OLD_CODE
            section = TDS_OLD_CODE if stale else TDS_NEW_CODE
            bps = TDS_OLD_BPS if stale else TDS_NEW_BPS
            record.tds = TdsEntry(
                payment_id=payment_id,
                section_code=section,
                rate_bps=bps,
                amount=apply_bps(ing["amount"], bps),
                deducted_at=_ts(settled),
            )

        records.append(record)
        ground_truth[payment_id] = truth_for(code)

    # --- bank credits ----------------------------------------------------
    #
    # Each UTR credits exactly the sum of its settlement lines, to the paise.
    # That is deliberate and it is the argument in CLAUDE.md section 1.3: the
    # settlement balances perfectly at the net level and still contains twelve
    # problems. Layer 1 and Layer 2 both pass. Only Layer 3 fails.
    totals: dict[str, list[int]] = {}
    for record in records:
        for line in record.settlement_lines:
            bucket = totals.setdefault(line.utr, [0, line.settled_at])
            bucket[0] += line.amount - line.fee - line.tax
            bucket[1] = max(bucket[1], line.settled_at)

    bank_credits = [
        BankCredit(utr=utr, amount=amount, credited_at=at)
        for utr, (amount, at) in sorted(totals.items())
    ]

    batch = Batch(records=records, bank_credits=bank_credits, seed=seed, rate_card=rc)
    return batch, ground_truth


# --- a look at what came out ---------------------------------------------

def summarise(batch: Batch, ground_truth: dict[str, str]) -> str:
    """Human-readable proof that the batch is what it claims to be."""
    counts: dict[str, int] = {}
    for code in ground_truth.values():
        counts[code] = counts.get(code, 0) + 1

    gross = sum(r.payment.amount for r in batch.records)
    fees = sum(ln.fee for r in batch.records for ln in r.settlement_lines)
    taxes = sum(ln.tax for r in batch.records for ln in r.settlement_lines)
    refunded = sum(r.refund.amount for r in batch.records if r.refund)
    credited = sum(bc.amount for bc in batch.bank_credits)

    anomalies = sum(v for k, v in counts.items()
                    if k not in DECOY_CODES and k != "CLEAN")
    decoys = sum(v for k, v in counts.items() if k in DECOY_CODES)

    lines = [
        f"batch of {len(batch.records)} records, seed {batch.seed}",
        f"  {anomalies} planted anomalies, {decoys} decoys, {counts.get('CLEAN', 0)} clean",
        "",
        "composition (the answer key):",
    ]
    for code in canonical_truth_mix():
        if code in counts:
            tag = ""
            if code in RECOVERABLE_CODES:
                tag = "  <- recoverable"
            elif code in DECOY_CODES:
                tag = "  <- decoy, must NOT be flagged"
            elif code == "TDS_CODE_MISMATCH":
                tag = "  <- tax credit at risk"
            elif code == "UNEXPLAINED":
                tag = "  <- must be refused, not explained"
            lines.append(f"  {counts[code]:>3} x {code}{tag}")

    lines += [
        "",
        f"gross sales        {rupees(gross):>16}",
        f"gateway fees       {rupees(-fees):>16}",
        f"GST on fees        {rupees(-taxes):>16}",
        f"refunds            {rupees(-refunded):>16}",
        f"bank credits       {rupees(credited):>16}  across {len(batch.bank_credits)} UTRs",
        "",
        f"settlements: {len(batch.settlement_ids)}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    b, gt = generate_batch(args.n, args.seed)
    print(summarise(b, gt))
