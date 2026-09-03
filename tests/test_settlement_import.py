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
def shop_razorpay(tmp_path, monkeypatch):
    """A signed-in business whose source is Razorpay, which is what makes the
    import card render at all."""
    from fastapi.testclient import TestClient

    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"name": "D", "email": "d@x.in",
                                 "password": "a-long-password"})
    client.post("/businesses", data={"name": "Data Panel"})
    with appmod.ledger() as led:
        business_id = led.businesses.all()[0]["business_id"]
        led.conn.execute(
            "INSERT OR REPLACE INTO data_sources (business_id, kind,"
            " razorpay_key_id, last_status, last_message)"
            " VALUES (?,'razorpay','rzp_test_x','ok','Connected.')",
            (business_id,))
        led.conn.commit()
    return client


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


# --- the Payments API fallback ---------------------------------------------

def _api(**over) -> dict:
    base = {"id": "pay_api1", "amount": 500_000, "status": "captured",
            "captured": True, "method": "upi", "fee": 10_000, "tax": 1_800,
            "created_at": 1_787_900_000, "order_id": "order_1",
            "amount_refunded": 0, "international": False}
    base.update(over)
    return base


def test_captured_payments_carry_everything_the_rate_check_needs():
    """Test mode never settles, so this is the only real Razorpay data a test
    account can ever produce."""
    from merchant.settlement_import import batch_from_payments

    batch = batch_from_payments(
        [_api(method="card", card={"network": "Visa", "type": "credit"})],
        {}).batch

    payment = batch.records[0].payment
    assert payment.method == "card"
    assert payment.card_network == "visa", "nested card details were missed"
    assert payment.card_type == "credit"
    assert batch.records[0].settlement_lines[0].fee == 10_000


def test_an_uncaptured_payment_is_skipped_and_named():
    from merchant.settlement_import import batch_from_payments

    result = batch_from_payments(
        [_api(), _api(id="pay_api2", captured=False, status="authorized")], {})

    assert result.payments == 1
    assert any("pay_api2" in s for s in result.skipped)


def test_the_payments_source_invents_no_settlement_date_or_bank_credit():
    """
    It answers "was I charged the right rate", not "did the money arrive".
    A fabricated settled_at would let a timing agent read this and report
    delays that were never measured.
    """
    from merchant.settlement_import import batch_from_payments

    batch = batch_from_payments([_api()], {}).batch

    assert batch.bank_credits == [], "invented a credit that never arrived"
    line = batch.records[0].settlement_lines[0]
    assert line.settled_at == 0 and not line.utr


def test_a_vpa_is_not_treated_as_a_upi_reference():
    """`vpa` is the payer's address. It says the rail was UPI, which `method`
    already says - it is not the cross-field evidence rule 9 needs."""
    from merchant.settlement_import import batch_from_payments

    batch = batch_from_payments(
        [_api(method="card", vpa="someone@okhdfcbank",
              card={"network": "visa", "type": "credit"})], {}).batch

    assert batch.records[0].payment.upi_reference is None


def test_payments_from_the_api_audit_like_any_other_batch(led):
    from merchant.settlement_import import batch_from_payments

    card = led.rate_card()
    result = batch_from_payments([
        _api(id="pay_upi", method="upi", amount=500_000, fee=10_000, tax=1_800),
    ], card)
    run_id = led.commit_settlement(result.batch)

    variance = detect_batch(led.load_batch(run_id, card))[0]
    assert variance.expected_fee < variance.actual_fee, (
        "a UPI payment charged at a card rate was not flagged")


# --- the data panel tells you whether anything arrived ----------------------

def test_the_data_panel_says_when_nothing_has_been_imported(shop_razorpay):
    """
    "My transactions are not showing up" was unanswerable from this page: it
    offered a Sync button and no statement of what, if anything, had landed.
    """
    page = shop_razorpay.get("/data").text

    assert "Import from Razorpay" in page
    assert "Nothing imported yet" in page
    assert "captured" in page, "the authorised-vs-captured trap is not named"


def test_the_data_panel_counts_what_did_arrive(shop_razorpay):
    import merchant.app as appmod
    from merchant.settlement_import import batch_from_payments

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        result = batch_from_payments(
            [_api(id=f"pay_{i}") for i in range(4)], led.rate_card())
        led.commit_settlement(result.batch)

    page = shop_razorpay.get("/data").text

    assert "4 payments imported" in page
    assert "Nothing imported yet" not in page
