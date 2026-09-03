"""
A simulated payment gateway. The thing being audited.

Everything else in this project audits a settlement file. Something has to
PRODUCE one, and in a live demo that something cannot be a fixture - the whole
point is typing in a transaction nobody has seen before.

## Why the misbehaviour is a visible setting rather than a hidden trick

The auditor is only interesting when there is something to catch. A demo where
the gateway always behaves correctly shows an empty report; a demo where errors
appear by magic is a magic trick.

So the gateway has a BEHAVIOUR, shown on screen and switchable mid-demo. Each
setting corresponds to something merchants actually report:

  CORRECT              charges exactly what the rate card says
  CARD_RATE_ON_UPI     applies a card MDR to a zero-MDR rail (the Trustpilot
                       complaint: "charged 8% conversion charges, which are not
                       supposed to be charged")
  OVER_CONTRACT        charges above the contracted slab by a margin
  GST_ON_SALE_VALUE    computes 18% GST on the transaction instead of the fee
  MISLABEL_UPI         records a UPI payment as a card and prices it as one

Nothing here imports the auditor. The gateway does not know what the correct
answer is, and the auditor does not know what the gateway was told to do - they
meet only through the settlement file, exactly as they would in reality.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class Behaviour(StrEnum):
    CORRECT = "correct"
    CARD_RATE_ON_UPI = "card_rate_on_upi"
    OVER_CONTRACT = "over_contract"
    GST_ON_SALE_VALUE = "gst_on_sale_value"
    MISLABEL_UPI = "mislabel_upi"


BEHAVIOUR_LABEL = {
    Behaviour.CORRECT:
        "Charge correctly, exactly per the rate card",
    Behaviour.CARD_RATE_ON_UPI:
        "Apply a 0.90% network MDR to UPI and RuPay (mandated to zero)",
    Behaviour.OVER_CONTRACT:
        "Charge 0.40% above the contracted slab on cards",
    Behaviour.GST_ON_SALE_VALUE:
        "Compute GST on the sale value instead of on the fee",
    Behaviour.MISLABEL_UPI:
        "Record UPI payments as cards and price them at the card rate",
}

# Which rails each fault actually touches.
#
# Not cosmetic. Set "card rate on UPI", take a Visa credit payment, and the
# auditor correctly finds nothing - which looks like the auditor failing unless
# you already knew the fault does not apply to that rail. Saying so on the
# picker removes the whole confusion.
BEHAVIOUR_AFFECTS = {
    Behaviour.CORRECT: [],
    Behaviour.CARD_RATE_ON_UPI: ["UPI", "RuPay debit"],
    Behaviour.OVER_CONTRACT: ["Visa/Mastercard debit", "Visa/Mastercard credit",
                              "Amex", "Netbanking", "Wallet"],
    Behaviour.GST_ON_SALE_VALUE: ["every instrument"],
    Behaviour.MISLABEL_UPI: ["UPI"],
}

BEHAVIOUR_FINDS = {
    Behaviour.CORRECT: "nothing - a clean sheet",
    Behaviour.CARD_RATE_ON_UPI: "ZERO_MDR_VIOLATION",
    Behaviour.OVER_CONTRACT: "RATE_MISMATCH",
    Behaviour.GST_ON_SALE_VALUE: "GST_MISMATCH",
    Behaviour.MISLABEL_UPI: "INSTRUMENT_MISLABEL",
}

BEHAVIOUR_NOTE = {
    Behaviour.CORRECT:
        "The honest case. A clean settlement should produce no findings.",
    Behaviour.CARD_RATE_ON_UPI:
        "Zero network MDR on UPI and RuPay is statutory, not negotiated.",
    Behaviour.OVER_CONTRACT:
        "Small enough to look plausible on one line, material across a month.",
    Behaviour.GST_ON_SALE_VALUE:
        "Roughly fifty times the correct amount. The most expensive error type.",
    Behaviour.MISLABEL_UPI:
        "Leaves a zero arithmetic gap. Findable only by a cross-field check.",
}

# The gateway's own price list. Deliberately a SEPARATE copy of the numbers from
# config/rate_card.json - if the two were the same object, a demo of a rate
# mismatch would be impossible, because the gateway would be reading the
# merchant's contract to decide what to charge. Real gateways do not do that.
GATEWAY_RATES_BPS = {
    "upi": 40, "rupay_debit": 40,
    "debit_card_low": 40, "debit_card_high": 90,
    "credit_card": 200, "premium_card": 300, "international": 300,
    "netbanking": 200, "wallet": 200,
}
GST_BPS = 1800

ZERO_MDR = {"upi", "rupay_debit"}


@dataclass
class Capture:
    """What the gateway recorded, and what it deducted. Its version of events."""
    method: str
    card_network: Optional[str]
    card_type: Optional[str]
    is_international: bool
    upi_reference: Optional[str]
    fee: int                  # paise
    tax: int                  # paise
    instrument_used: str      # the gateway's own name for the rail


def _bps(amount: int, rate: int) -> int:
    """Half-up to whole paise. Integer arithmetic, same as everywhere else."""
    return (amount * rate + 5_000) // 10_000


def instrument_for(method: str, card_network: Optional[str],
                   card_type: Optional[str], is_international: bool,
                   amount: int) -> str:
    if method == "upi":
        return "upi"
    if method == "netbanking":
        return "netbanking"
    if method == "wallet":
        return "wallet"
    if method == "card":
        network = (card_network or "").lower()
        if is_international:
            return "international"
        if network in ("amex", "diners"):
            return "premium_card"
        if network == "rupay" and card_type == "debit":
            return "rupay_debit"
        if card_type == "debit":
            return "debit_card_low" if amount <= 200_000 else "debit_card_high"
        return "credit_card"
    return "credit_card"


def capture(amount: int, method: str, behaviour: Behaviour,
            card_network: Optional[str] = None, card_type: Optional[str] = None,
            is_international: bool = False,
            rng: Optional[random.Random] = None) -> Capture:
    """
    Take a payment and decide what to deduct for it.

    This function has no idea what is correct. It applies its own price list and
    whatever behaviour it has been configured with, and the auditor finds out
    later - which is the actual relationship between a merchant and a gateway.
    """
    rng = rng or random.Random()
    instrument = instrument_for(method, card_network, card_type,
                                is_international, amount)

    upi_reference = None
    if method == "upi":
        upi_reference = "".join(rng.choice("0123456789") for _ in range(12))

    recorded_method = method
    recorded_network = card_network
    recorded_type = card_type

    # --- what gets charged ------------------------------------------------
    if behaviour == Behaviour.MISLABEL_UPI and method == "upi":
        # The rail was UPI. The record says card, and the card rate is applied.
        # The UPI reference survives, which is the only trace left.
        recorded_method = "card"
        recorded_network = "visa"
        recorded_type = "credit"
        instrument = "credit_card"
        fee = _bps(amount, GATEWAY_RATES_BPS["credit_card"])

    elif behaviour == Behaviour.CARD_RATE_ON_UPI and instrument in ZERO_MDR:
        fee = _bps(amount, GATEWAY_RATES_BPS[instrument]) + _bps(amount, 90)

    elif behaviour == Behaviour.OVER_CONTRACT and instrument not in ZERO_MDR:
        fee = _bps(amount, GATEWAY_RATES_BPS[instrument] + 40)

    else:
        fee = _bps(amount, GATEWAY_RATES_BPS[instrument])

    # --- and the GST on it ------------------------------------------------
    if behaviour == Behaviour.GST_ON_SALE_VALUE:
        tax = _bps(amount, GST_BPS)          # the wrong base, on purpose
    else:
        tax = _bps(fee, GST_BPS)

    return Capture(method=recorded_method, card_network=recorded_network,
                   card_type=recorded_type, is_international=is_international,
                   upi_reference=upi_reference, fee=fee, tax=tax,
                   instrument_used=instrument)


SEPARATOR = ","


def parse_behaviours(text) -> list["Behaviour"]:
    """
    The stored gateway setting as a list.

    Mirrors merchant.suppliers.parse_behaviours exactly, for the same reason:
    a row written before the simulator could hold several faults contains a
    bare value, and that has to keep parsing as a one-element list rather
    than needing a migration. Unknown values are dropped instead of raising -
    a setting a merchant cannot correct from the UI must not be able to break
    their simulator.
    """
    if not text:
        return [Behaviour.CORRECT]
    if isinstance(text, Behaviour):
        return [text]
    if isinstance(text, str):
        parts = [p.strip() for p in text.split(SEPARATOR)]
    else:
        parts = [str(p).strip() for p in text]

    out = []
    for part in parts:
        try:
            found = Behaviour(part)
        except ValueError:
            continue
        if found not in out:
            out.append(found)
    return out or [Behaviour.CORRECT]


def join_behaviours(chosen) -> str:
    """The list as it is stored. Order follows the enum so it is stable."""
    kept = [b for b in Behaviour if b in set(parse_behaviours(chosen))]
    return SEPARATOR.join(str(b) for b in kept)
