"""
Expected-value engine.

Given a payment, computes what the gateway SHOULD have charged.
Pure arithmetic. No LLM involved anywhere in this file - that is deliberate.
See CLAUDE.md section 2.

ALL MONEY IS INTEGER PAISE. Never floats. Convert to rupees only for display.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

RATE_CARD_PATH = Path(__file__).parent.parent / "config" / "rate_card.json"

DEBIT_TICKET_THRESHOLD_PAISE = 200_000  # Rs 2,000 - the RBI cap boundary
ZERO_MDR_NETWORKS = {"rupay"}
PREMIUM_NETWORKS = {"amex", "diners"}


def load_rate_card(path: Path = RATE_CARD_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


@dataclass
class Payment:
    """Mirrors Razorpay's payment fields. See CLAUDE.md section 9."""
    payment_id: str
    amount: int                          # paise
    method: str                          # upi | card | netbanking | wallet | emi
    card_network: Optional[str] = None   # visa | mastercard | rupay | amex | diners
    card_type: Optional[str] = None      # debit | credit
    is_international: bool = False
    upi_reference: Optional[str] = None  # RRN/UMN - drives rule 9


@dataclass
class FeeBreakdown:
    """What the fee should have been, and why."""
    instrument_key: str
    instrument_label: str
    network_mdr_paise: int
    platform_fee_paise: int
    total_fee_paise: int
    gst_paise: int
    total_deduction_paise: int
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def net_to_merchant_paise(self) -> int:
        return -self.total_deduction_paise  # caller applies against amount


def classify_instrument(payment: Payment) -> tuple[str, list[str]]:
    """
    Map a payment onto a rate-card instrument key.

    Returns (instrument_key, notes). Notes flag anything suspicious that the
    agent should look at later - this function never decides it's an error,
    it only records what it observed.
    """
    notes: list[str] = []

    if payment.method == "upi":
        return "upi", notes

    if payment.method == "card":
        network = (payment.card_network or "").lower()

        # Rule 9 input: a card-labelled payment carrying a UPI reference is
        # the classic mislabel signature. Flag it; do not judge it here.
        if payment.upi_reference:
            notes.append(
                "method='card' but a UPI reference is present - possible "
                "instrument mislabel (rule 9)"
            )

        if payment.is_international:
            return "international", notes

        if network in PREMIUM_NETWORKS:
            return "premium_card", notes

        if network in ZERO_MDR_NETWORKS and payment.card_type == "debit":
            return "rupay_debit", notes

        if payment.card_type == "debit":
            if payment.amount <= DEBIT_TICKET_THRESHOLD_PAISE:
                return "debit_card_low", notes
            return "debit_card_high", notes

        return "credit_card", notes

    if payment.method == "netbanking":
        return "netbanking", notes

    if payment.method == "wallet":
        return "wallet", notes

    if payment.method == "emi":
        return "premium_card", notes

    notes.append(f"unrecognised method '{payment.method}' - defaulted to credit slab")
    return "credit_card", notes


def _bps(amount_paise: int, rate_bps: int) -> int:
    """
    Apply a basis-point rate to an amount, rounding half-up to whole paise.

    Integer arithmetic throughout. 10_000 bps = 100%.
    """
    return (amount_paise * rate_bps + 5_000) // 10_000


def compute_expected_fee(payment: Payment, rate_card: dict) -> FeeBreakdown:
    """The core function. Returns what SHOULD have been charged."""
    key, notes = classify_instrument(payment)
    spec = rate_card["instruments"][key]

    # --- network MDR, subject to any regulatory cap ---------------------
    network_bps = spec["network_mdr_bps"]
    cap_bps = spec.get("network_mdr_cap_bps")
    if cap_bps is not None and network_bps > cap_bps:
        notes.append(
            f"contracted network rate {network_bps}bps exceeds regulatory cap "
            f"{cap_bps}bps - cap applied"
        )
        network_bps = cap_bps

    network_mdr = _bps(payment.amount, network_bps)

    # --- platform fee: legal even on zero-MDR rails ---------------------
    # This is the distinction that matters. UPI network MDR is mandated to
    # zero; the gateway's own platform fee is NOT and remains chargeable.
    platform_fee = _bps(payment.amount, spec["platform_fee_bps"])

    total_fee = network_mdr + platform_fee

    # --- GST: 18% OF THE FEE, never of the transaction value ------------
    gst = _bps(total_fee, rate_card["gst_rate_bps"])

    sources = [spec["network_mdr_source"]]
    if spec["platform_fee_bps"] > 0:
        sources.append(spec["platform_fee_source"])
    sources.append(rate_card["gst_source"])

    return FeeBreakdown(
        instrument_key=key,
        instrument_label=spec["label"],
        network_mdr_paise=network_mdr,
        platform_fee_paise=platform_fee,
        total_fee_paise=total_fee,
        gst_paise=gst,
        total_deduction_paise=total_fee + gst,
        sources=sources,
        notes=notes,
    )


def tolerance_paise(expected_fee_paise: int, rate_card: dict) -> int:
    """
    How big a gap counts as an exception.
    max(Rs 1, 0.5% of expected fee). See CLAUDE.md section 6.2.
    """
    tol = rate_card["tolerance"]
    return max(tol["floor_paise"], _bps(expected_fee_paise, tol["pct_bps"]))


def rupees(paise: int) -> str:
    """
    Display helper. Only place paise become a decimal.

    Delegates rather than formatting, because there were two of these and they
    disagreed: this one grouped in thousands and engine/gst/rules grouped in
    lakhs, so the settlement half of the product printed Rs 1,000,000.00 on
    the same screen where the GST half printed Rs 10,00,000.00. Two formats
    for the same quantity in one Indian finance product is a defect, and the
    lakh grouping is the one a merchant here reads without counting digits.

    Kept as a name rather than collapsed into an import, because two dozen
    modules call it and the indirection costs nothing.
    """
    from engine.gst.rules import rupees as indian

    return indian(paise)


# --- the inverse of classify_instrument ---------------------------------
#
# classify_instrument turns Razorpay's fields into a rate-card key. Sometimes
# we need to go the other way: "what would this sale have cost as UPI?" That
# question is how an instrument mislabel gets priced, and it is also how the
# synthetic generator builds a payment for a chosen instrument.
#
# Kept next to classify_instrument on purpose. If a new instrument is added to
# the rate card, the two halves are in the same file and it is obvious that
# both need updating.

INSTRUMENT_FIELDS: dict[str, dict] = {
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
}


def reprice_as(payment: Payment, instrument_key: str, rate_card: dict) -> FeeBreakdown:
    """
    What would this same sale have cost, priced as a different instrument?

    Used to put a rupee figure on a mislabel: a UPI payment tagged as a card is
    charged the card rate, and the recoverable amount is the difference. Pure
    arithmetic - the agent is told the number, it never works it out.
    """
    if instrument_key not in INSTRUMENT_FIELDS:
        raise KeyError(f"unknown instrument key {instrument_key!r}")
    probe = Payment(
        payment_id=payment.payment_id,
        amount=payment.amount,
        **INSTRUMENT_FIELDS[instrument_key],
    )
    return compute_expected_fee(probe, rate_card)


# --- settlement timing ---------------------------------------------------

SETTLEMENT_WORKING_DAYS = 2      # Razorpay standard is T+2 working days


def add_working_days(dt, days: int):
    """Weekends do not count towards a settlement cycle."""
    from datetime import timedelta
    out = dt
    remaining = days
    while remaining > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            remaining -= 1
    return out
