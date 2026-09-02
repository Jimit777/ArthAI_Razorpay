"""
Tests for turning Razorpay's settlement recon report into an auditable batch.

The point of this layer is that nothing downstream can tell an imported batch
from a generated one - so most of these check that the shape is right and the
arithmetic survives, and the rest check what it refuses to invent.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.detector import detect_batch  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402
from merchant.settlement_import import batch_from_recon  # noqa: E402


def _row(**over) -> dict:
    base = {
        "entity_id": "pay_1", "type": "payment", "payment_id": "pay_1",
        "order_id": "order_1", "amount": 500_000, "fee": 10_000, "tax": 1_800,
        "created_at": 1_787_900_000, "settled_at": 1_788_000_000,
        "settlement_id": "setl_1", "settlement_utr": "UTR1", "method": "upi",
    }
    base.update(over)
    return base


@pytest.fixture
def led(tmp_path):
    boot = Ledger(tmp_path / "i.db")
    business_id = boot.businesses.create("Import Test")
    boot.close()
    handle = Ledger(tmp_path / "i.db", business_id)
    yield handle
    handle.close()


# --- the shape --------------------------------------------------------------

def test_a_recon_row_becomes_a_priceable_payment():
    """method, card_network and card_type are what let the engine work out
    what the fee should have been. Without them there is nothing to check."""
    batch = batch_from_recon(
        [_row(method="card", card_network="Visa", card_type="Credit")],
        {}).batch

    payment = batch.records[0].payment
    assert payment.method == "card"
    assert payment.card_network == "visa", "network was not normalised"
    assert payment.card_type == "credit"
    assert payment.amount == 500_000


def test_a_refund_attaches_to_its_payment_rather_than_a_second_record():
    """Rule 8 - the fee is retained on a refund - only works if the refund
    and the payment it reverses are the same record."""
    result = batch_from_recon(
        [_row(), _row(entity_id="rfnd_1", type="refund", amount=100_000,
                      fee=0, tax=0)], {})

    assert len(result.batch.records) == 1, "the refund became its own record"
    record = result.batch.records[0]
    assert record.refund is not None
    assert len(record.settlement_lines) == 2
    assert result.payments == 1 and result.refunds == 1


def test_a_refund_reduces_the_bank_credit_rather_than_adding_to_it():
    """
    Regression. The report states refund amounts as positive magnitudes and
    marks them as debits, so adding one overstated what reached the bank by
    twice the refund.
    """
    result = batch_from_recon(
        [_row(amount=500_000, fee=1_000, tax=180),
         _row(entity_id="rfnd_1", type="refund", amount=100_000, fee=0, tax=0)],
        {})

    assert result.batch.bank_credits[0].amount == (500_000 - 1_000 - 180
                                                   - 100_000)


def test_transfers_and_adjustments_are_named_not_silently_dropped():
    """Both are real rows with no rate to check. Counted so the merchant is
    told the import did not cover them."""
    result = batch_from_recon(
        [_row(), {"entity_id": "adj_1", "type": "adjustment", "amount": -500},
         {"entity_id": "trf_1", "type": "transfer", "amount": 900}], {})

    assert result.payments == 1
    assert len(result.skipped) == 2
    assert any("adj_1" in s for s in result.skipped)


def test_nothing_auditable_returns_no_batch():
    result = batch_from_recon(
        [{"entity_id": "adj_1", "type": "adjustment", "amount": -500}], {})

    assert not result.ok and result.batch is None
    result = batch_from_recon([], {})
    assert not result.ok


# --- what it refuses to invent ---------------------------------------------

def test_the_upi_reference_is_left_empty_rather_than_guessed():
    """
    Rule 9 accuses a gateway of pricing a UPI payment as a card. The recon
    report has no RRN/UMN field, and `description`/`notes` are free text the
    merchant controls - deriving a payment rail from a memo and then making
    that accusation is exactly the confident wrongness this product avoids.
    """
    batch = batch_from_recon(
        [_row(description="UPI RRN 446123464299", notes="upi ref 12345")],
        {}).batch

    assert batch.records[0].payment.upi_reference is None


def test_an_unstated_method_is_not_assumed():
    batch = batch_from_recon([_row(method=None)], {}).batch
    assert batch.records[0].payment.method == "unknown"


def test_an_imported_batch_carries_no_seed():
    """A seed would suggest these rows could be regenerated from one."""
    assert batch_from_recon([_row()], {}).batch.seed == 0


# --- and it audits ----------------------------------------------------------

def test_imported_rows_are_audited_exactly_like_generated_ones(led):
    """
    The whole point of matching the generated shape: a UPI payment charged at
    a card rate has to surface from imported data the same way it does from
    the simulator.
    """
    card = led.rate_card()
    result = batch_from_recon([
        _row(payment_id="pay_upi", entity_id="pay_upi", method="upi",
             amount=500_000, fee=10_000, tax=1_800),
        _row(payment_id="pay_card", entity_id="pay_card", order_id="order_2",
             method="card", card_network="visa", card_type="credit",
             amount=300_000, fee=6_000, tax=1_080),
    ], card)

    run_id = led.commit_settlement(result.batch)
    variances = {v.payment_id: v for v in detect_batch(
        led.load_batch(run_id, card))}

    assert variances["pay_upi"].expected_fee < variances["pay_upi"].actual_fee, (
        "a UPI payment charged at a card rate was not flagged")
    assert variances["pay_card"].delta == 0, "a correctly priced card moved"
