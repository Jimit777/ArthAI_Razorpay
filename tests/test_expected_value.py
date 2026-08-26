"""
One test per rule. Each asserts a number that a regulator or a contract
determined - not a number we invented.

Run: python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.expected_value import (  # noqa: E402
    Payment,
    classify_instrument,
    compute_expected_fee,
    load_rate_card,
    rupees,
    tolerance_paise,
)

RC = load_rate_card()


# --- Rule 1: UPI network MDR = 0% ---------------------------------------

def test_rule1_upi_network_mdr_is_zero():
    p = Payment(payment_id="pay_1", amount=400_000, method="upi")  # Rs 4,000
    fb = compute_expected_fee(p, RC)
    assert fb.network_mdr_paise == 0


def test_rule1_upi_platform_fee_is_still_chargeable():
    """The trap. Zero network MDR does NOT mean zero fee."""
    p = Payment(payment_id="pay_2", amount=400_000, method="upi")
    fb = compute_expected_fee(p, RC)
    assert fb.platform_fee_paise == 1_600   # 0.40% of Rs 4,000 = Rs 16
    assert fb.total_fee_paise == 1_600
    assert rupees(fb.total_fee_paise) == "Rs 16.00"


# --- Rule 2: RuPay debit network MDR = 0% -------------------------------

def test_rule2_rupay_debit_network_mdr_is_zero():
    p = Payment(
        payment_id="pay_3", amount=500_000, method="card",
        card_network="rupay", card_type="debit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.instrument_key == "rupay_debit"
    assert fb.network_mdr_paise == 0


# --- Rules 3 & 4: debit card caps at the Rs 2,000 boundary --------------

def test_rule3_debit_at_or_below_2000_capped_at_40bps():
    p = Payment(
        payment_id="pay_4", amount=200_000, method="card",   # exactly Rs 2,000
        card_network="visa", card_type="debit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.instrument_key == "debit_card_low"
    assert fb.total_fee_paise == 800          # 0.40% of Rs 2,000 = Rs 8


def test_rule4_debit_above_2000_capped_at_90bps():
    p = Payment(
        payment_id="pay_5", amount=200_001, method="card",   # one paise over
        card_network="visa", card_type="debit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.instrument_key == "debit_card_high"
    assert fb.total_fee_paise == 1_800        # 0.90% of Rs 2,000.01


def test_boundary_is_inclusive_at_2000():
    """One paise decides which RBI cap applies. Worth pinning down."""
    low, _ = classify_instrument(Payment("a", 200_000, "card", "visa", "debit"))
    high, _ = classify_instrument(Payment("b", 200_100, "card", "visa", "debit"))
    assert low == "debit_card_low"
    assert high == "debit_card_high"


# --- Rules 5 & 6: credit and premium slabs ------------------------------

def test_rule5_credit_card_uses_contracted_slab():
    p = Payment(
        payment_id="pay_6", amount=350_000, method="card",
        card_network="visa", card_type="credit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.total_fee_paise == 7_000        # 2% of Rs 3,500 = Rs 70


def test_rule6_amex_routes_to_premium_slab():
    p = Payment(
        payment_id="pay_7", amount=350_000, method="card",
        card_network="amex", card_type="credit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.instrument_key == "premium_card"
    assert fb.total_fee_paise == 10_500       # 3% of Rs 3,500 = Rs 105


def test_rule6_international_routes_to_international_slab():
    p = Payment(
        payment_id="pay_8", amount=350_000, method="card",
        card_network="visa", card_type="credit", is_international=True,
    )
    fb = compute_expected_fee(p, RC)
    assert fb.instrument_key == "international"


# --- Rule 7: GST is 18% OF THE FEE, not of the transaction --------------

def test_rule7_gst_is_on_fee_not_transaction_value():
    p = Payment(
        payment_id="pay_9", amount=350_000, method="card",
        card_network="visa", card_type="credit",
    )
    fb = compute_expected_fee(p, RC)
    assert fb.total_fee_paise == 7_000
    assert fb.gst_paise == 1_260              # 18% of Rs 70 = Rs 12.60
    # The catastrophic wrong answer would be 18% of Rs 3,500 = Rs 630
    assert fb.gst_paise != 63_000


def test_rule7_gst_follows_total_fee_including_platform_fee_on_zero_mdr_rails():
    """
    THE TRAP, pinned down.

    RuPay debit carries zero NETWORK MDR by mandate - but the gateway's
    platform fee is still legally chargeable, and GST follows that fee.
    Asserting gst == 0 here would encode the naive reading of the rule and
    make the agent under-expect the deduction on every UPI/RuPay payment.
    """
    p = Payment(payment_id="pay_10", amount=100_000, method="card",  # Rs 1,000
                card_network="rupay", card_type="debit")
    fb = compute_expected_fee(p, RC)
    assert fb.network_mdr_paise == 0      # mandated
    assert fb.platform_fee_paise == 400   # 0.40% of Rs 1,000 - legal
    assert fb.gst_paise == 72             # 18% of Rs 4.00


def test_rule7_a_genuinely_zero_fee_produces_zero_gst():
    zero_card = {**RC, "instruments": {
        **RC["instruments"],
        "upi": {**RC["instruments"]["upi"], "platform_fee_bps": 0},
    }}
    fb = compute_expected_fee(Payment("pay_10b", 100_000, "upi"), zero_card)
    assert fb.total_fee_paise == 0
    assert fb.gst_paise == 0


# --- Rule 9: instrument mislabel signature ------------------------------

def test_rule9_card_labelled_payment_with_upi_reference_is_noted():
    p = Payment(
        payment_id="pay_11", amount=400_000, method="card",
        card_network="visa", card_type="credit",
        upi_reference="123456789012",
    )
    fb = compute_expected_fee(p, RC)
    assert any("mislabel" in n for n in fb.notes)


def test_rule9_clean_card_payment_raises_no_note():
    p = Payment("pay_12", 400_000, "card", "visa", "credit")
    fb = compute_expected_fee(p, RC)
    assert fb.notes == []


# --- Tolerance band ------------------------------------------------------

def test_tolerance_floor_applies_to_small_fees():
    assert tolerance_paise(1_600, RC) == 100          # Rs 1 floor wins


def test_tolerance_percentage_applies_to_large_fees():
    assert tolerance_paise(100_000, RC) == 500        # 0.5% of Rs 1,000 wins


# --- The worked example from the pitch ----------------------------------

def test_meera_boutique_end_to_end():
    """
    Five Monday orders totalling Rs 9,000. This is the demo narrative -
    if this drifts, the pitch drifts with it.
    """
    orders = [
        Payment("pay_1001", 120_000, "upi"),                            # Rs 1,200
        Payment("pay_1002", 350_000, "card", "visa", "credit"),         # Rs 3,500
        Payment("pay_1003", 80_000, "upi"),                             # Rs   800
        Payment("pay_1004", 200_000, "card", "visa", "debit"),          # Rs 2,000
        Payment("pay_1005", 150_000, "upi"),                            # Rs 1,500
    ]
    gross = sum(o.amount for o in orders)
    assert gross == 900_000                                             # Rs 9,000

    breakdowns = [compute_expected_fee(o, RC) for o in orders]
    total_fee = sum(b.total_fee_paise for b in breakdowns)
    total_gst = sum(b.gst_paise for b in breakdowns)

    # UPI: 0.40% of 1200 + 800 + 1500 = Rs 14.00
    # Credit card: 2% of 3500 = Rs 70.00
    # Debit <= 2000: 0.40% of 2000 = Rs 8.00
    assert total_fee == 9_200                                           # Rs 92.00
    assert total_gst == 1_656                                           # Rs 16.56

    refund = 150_000                                                    # order 1005
    net = gross - total_fee - total_gst - refund
    assert rupees(net) == "Rs 7,391.44"


# --- one money format, not two --------------------------------------------

def test_there_is_only_one_rupee_format_in_the_product():
    """
    There were two, and they disagreed.

    engine/expected_value grouped in thousands and engine/gst/rules grouped in
    lakhs, so the settlement half of the product printed Rs 1,000,000.00 on
    the same screen where the GST half printed Rs 10,00,000.00 - and the cash
    forecaster's hover readout showed both, one line apart. Two formats for
    the same quantity in one Indian finance product is a defect.
    """
    from engine.expected_value import rupees as settlement_side
    from engine.gst.rules import rupees as gst_side

    for paise in (0, 5, 99, 100, 668900, 94000000, 100000000, -12345678,
                  1_00_00_000_00):
        assert settlement_side(paise) == gst_side(paise), paise


def test_money_is_grouped_the_way_an_indian_merchant_reads_it():
    """Lakhs and crores, not thousands. Rs 12,34,567.89."""
    from engine.expected_value import rupees

    assert rupees(123456789) == "Rs 12,34,567.89"
    assert rupees(100000000) == "Rs 10,00,000.00"
    assert rupees(-100000) == "-Rs 1,000.00"
