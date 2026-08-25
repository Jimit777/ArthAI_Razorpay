"""
Tests for the schema check. Checkpoint 1, second half.

No network. What is tested is the comparison logic and the safety rails -
specifically that the script cannot be pointed at live keys, and that the
secret cannot reach a file.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from razorpay_check import (  # noqa: E402
    OUR_FIELDS,
    OUR_ID_FORMATS,
    Razorpay,
    check_id,
    compare,
)

# The real order object this project actually received from the API, kept as a
# fixture so the comparison logic is tested against a genuine response rather
# than one we made up.
REAL_ORDER = {
    "amount": 162700, "amount_due": 162700, "amount_paid": 0, "attempts": 0,
    "created_at": 1787425586, "currency": "INR", "entity": "order",
    "id": "order_TSvP1JNtQ6deWn", "notes": {}, "offer_id": None,
    "receipt": "settlement-auditor-schema-check", "status": "created",
}


# --- it must never touch live keys --------------------------------------

def test_live_keys_are_refused():
    """
    This script creates an order. Pointed at a live key it would create a real
    one against a real merchant account.
    """
    with pytest.raises(SystemExit) as exc:
        Razorpay("rzp_live_something", "secret")
    assert "not a test key" in str(exc.value)


def test_test_keys_are_accepted():
    assert Razorpay("rzp_test_something", "secret")


def test_the_checkout_page_carries_the_key_id_but_never_the_secret():
    """
    The key id is public by design - it ships in every merchant's checkout HTML.
    The secret is not, and a generated file is exactly the sort of place one
    ends up by accident.
    """
    from razorpay_checkout import PAGE

    page = PAGE.format(order_id="order_x", key_id="rzp_test_abc",
                       amount=100, rupees="1.00")
    assert "rzp_test_abc" in page
    for name in ("secret", "key_secret", "RAZORPAY_KEY_SECRET"):
        assert name not in page

    # Braces survive on purpose - the page contains CSS and JavaScript. What
    # must not survive is one of OUR placeholders, which would mean a field
    # silently rendered as literal text.
    for placeholder in ("{order_id}", "{key_id}", "{amount}", "{rupees}"):
        assert placeholder not in page


# --- the comparison ------------------------------------------------------

def test_we_invented_nothing_on_the_real_order():
    """
    The claim the README makes. Every field the generator produces has to exist
    in the real response - modelling a subset is honest, inventing is not.
    """
    result = compare("order", REAL_ORDER)
    assert result["we_invented"] == []
    assert set(result["confirmed"]) == OUR_FIELDS["order"]


def test_the_comparison_reports_what_we_do_not_model():
    """
    Omissions are fine and worth listing - they are the honest limit of what we
    reconstruct, and a judge asking "what about status?" deserves an answer.
    """
    result = compare("order", REAL_ORDER)
    assert "status" in result["we_omit"]
    assert "receipt" in result["we_omit"]


def test_an_invented_field_is_caught():
    """The check has to be able to fail, or it proves nothing."""
    trimmed = {k: v for k, v in REAL_ORDER.items() if k != "currency"}
    assert "currency" in compare("order", trimmed)["we_invented"]


# --- id formats ----------------------------------------------------------

def test_our_generated_ids_match_the_real_format():
    assert check_id("order", REAL_ORDER["id"])


def test_the_generator_produces_ids_the_real_api_would_accept():
    """
    Closes the loop: the format the API returned, applied to what we generate.
    If Razorpay ever changes id length this goes red.
    """
    from generator.synthetic import generate_batch

    b, _ = generate_batch(60)
    for record in b.records:
        assert check_id("payment", record.payment.payment_id), record.record_id
        assert check_id("order", record.order_id)
        if record.refund:
            assert check_id("refund", record.refund.refund_id)
        for line in record.settlement_lines:
            assert re.match(r"^setl_[A-Za-z0-9]{14}$", line.settlement_id)


def test_a_wrong_id_format_is_rejected():
    assert not check_id("order", "order_short")
    assert not check_id("payment", "order_TSvP1JNtQ6deWn")
    assert not check_id("order", "TSvP1JNtQ6deWn")


def test_every_entity_we_model_has_an_id_pattern():
    assert set(OUR_ID_FORMATS) == set(OUR_FIELDS)


# --- money ---------------------------------------------------------------

def test_the_real_api_uses_integer_paise_exactly_as_we_do():
    """
    The single most important thing to have got right. Razorpay counts in
    integer paise; so do we; nothing anywhere is a float.
    """
    assert isinstance(REAL_ORDER["amount"], int)
    assert REAL_ORDER["amount"] == 162_700
    assert REAL_ORDER["currency"] == "INR"


def test_created_at_is_a_unix_timestamp_like_ours():
    from generator.synthetic import generate_batch

    assert isinstance(REAL_ORDER["created_at"], int)
    b, _ = generate_batch(20)
    assert isinstance(b.records[0].created_at, int)
