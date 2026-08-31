"""
Real disputes, pulled from Razorpay's own `GET /v1/disputes`, mapped into
this engine's Dispute shape.

Field names verified directly against razorpay.com/docs/api/disputes/entity/
and razorpay.com/docs/api/disputes/fetch-all/ this session (not from
memory): `id, payment_id, amount, currency, amount_deducted, reason_code,
reason_description, respond_by, status, phase, created_at, evidence`.

A dispute missing its own id, payment_id, reason_code or respond_by is
skipped and named, never defaulted - same "absence is not innocence"
discipline the GST filing Razorpay pull already applies to a missing buyer
address.
"""

from __future__ import annotations

from engine.chargeback.detector import Dispute


def from_razorpay_dispute(raw: dict) -> tuple[Dispute | None, str | None]:
    """One real dispute item -> (Dispute, None) or (None, reason)."""
    dispute_id = str(raw.get("id") or "").strip()
    payment_id = str(raw.get("payment_id") or "").strip()
    reason_code = str(raw.get("reason_code") or "").strip()
    respond_by = raw.get("respond_by")

    if not dispute_id:
        return None, "no dispute id"
    if not payment_id:
        return None, f"{dispute_id}: no payment_id"
    if not reason_code:
        return None, f"{dispute_id}: no reason_code"
    if not respond_by:
        return None, f"{dispute_id}: no respond_by deadline"

    return Dispute(
        dispute_id=dispute_id, payment_id=payment_id,
        amount_paise=int(raw.get("amount") or 0),
        reason_code=reason_code,
        reason_description=str(raw.get("reason_description") or ""),
        phase=str(raw.get("phase") or ""), status=str(raw.get("status") or ""),
        respond_by=int(respond_by)), None


def from_razorpay_batch(raw_items: list[dict]
                        ) -> tuple[list[Dispute], list[tuple[str, str]]]:
    disputes: list[Dispute] = []
    skipped: list[tuple[str, str]] = []
    for raw in raw_items:
        dispute, reason = from_razorpay_dispute(raw)
        if dispute is None:
            skipped.append((str(raw.get("id") or "?"), reason or "unknown"))
        else:
            disputes.append(dispute)
    return disputes, skipped
