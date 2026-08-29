"""
The tools that make this an ecosystem rather than four apps sharing a
database: before the cash forecaster tells a controller a receipt is safe to
count on, or that the month is comfortable, it can check what this
platform's OTHER agents already found.

Three connections live here:

  settlement_status(payment_id) - before counting a specific receipt,
  check whether the settlement auditor found something wrong with that
  exact payment.

  at_risk_input_credit() - before calling the month comfortable, check
  whether the GST input-credit reconciler found claimed credit that may
  have to be repaid. Not tied to any one receipt or payout - it is a
  standing liability, not a line item, so it takes no argument.

  recon_status(payment_id) - before counting a specific receipt, check
  whether the three-way reconciler already found that this exact
  settlement never reached the bank, or reached it as a different amount.
  Also offered to the three-way reconciler itself, so it can ask the
  settlement auditor the mirror question before raising an alarm - see
  agent/recon_agent.py.

## Why this lives in merchant/, not agent/

Every other tool an agent gets closes over data already in memory - a
forecast, a pool of exception rows. These need to open the shared database
and look up a business's OWN history from other agents, which only exists
once a run has been committed under a business_id. agent/ never imports
merchant/ (CLAUDE.md's calculator/judge split holds a layer boundary too:
the judgment layer does not know the storage layer exists), so these tools
are built here and handed to the classifier as extra tools rather than
built inside it.

## Why they open their own connection per call

The ledger used to gather this run's inputs is already closed by the time
the agent asks a question - tool calls happen after the pipeline that
assembled the evidence has already returned. A short-lived connection per
call is the same pattern the rest of this platform uses for anything a
request handler does not keep open (see merchant/app.py::ledger()).

## Why demo runs never get these tools

A demo forecast's payment ids and business_id are generated fresh for that
scenario - nothing with that identity exists in the settlement, GST or
reconciliation tables. Offering the tools there would mean the agent
"checks" and always finds nothing, which reads as a fact about the money
and is really just an artifact of the demo data being disconnected random
universes. Withholding the tools is more honest than permanently-empty ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from anthropic import beta_tool

DB = os.environ.get("AUDITOR_DB",
                    str(Path(__file__).parent.parent / "merchant.db"))


def _money(paise: int) -> dict:
    from engine.gst import rules

    return {"paise": paise, "display": rules.rupees(paise)}


def _lookup(business_id: str, payment_id: str) -> list[dict]:
    from merchant.ledger import Ledger

    led = Ledger(DB, business_id)
    try:
        rows = led.store.conn.execute(
            "SELECT v.run_id, v.exception_code, v.action, v.money_at_stake,"
            " v.reasoning, v.rule_cited, v.human_reviewed, v.queued_for_human"
            " FROM variances v JOIN business_runs br ON br.run_id = v.run_id"
            " WHERE br.business_id = ? AND v.payment_id = ?"
            " ORDER BY v.created_at DESC", (business_id, payment_id)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        led.close()


def _overclaimed(business_id: str) -> list[dict]:
    """
    This business's latest GST reconciliation, narrowed to credit it should
    stop claiming - not credit it is still owed. The two read alike at a
    glance and mean opposite things for cash: AT_RISK is money that will not
    arrive, which a receivable check would catch; OVERCLAIMED is money
    already banked against this quarter's GST bill that may have to be paid
    BACK, with interest, if nobody reverses it first. That is the only half
    of the ITC taxonomy a cash forecast has any business asking about.
    """
    from engine.gst.taxonomy import OVERCLAIMED
    from merchant.ledger import Ledger

    led = Ledger(DB, business_id)
    try:
        latest = led.conn.execute(
            "SELECT run_id FROM business_itc_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (business_id,)).fetchone()
        if not latest:
            return []
        codes = ",".join("?" * len(OVERCLAIMED))
        rows = led.conn.execute(
            "SELECT supplier_name, invoice_number, exception_code,"
            " money_at_stake, claim_deadline"
            f" FROM itc_findings WHERE business_id = ? AND run_id = ?"
            f" AND exception_code IN ({codes})"
            " ORDER BY money_at_stake DESC",
            (business_id, latest["run_id"], *[str(c) for c in OVERCLAIMED])
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        led.close()


def _recon_lookup(business_id: str, payment_id: str) -> list[dict]:
    """
    This business's LATEST reconciliation, narrowed to one payment.

    Only the latest run - an older run's finding could have been resolved by
    a more recent one, and counting both would double an amount that only
    exists once. Matched lines are never in recon_findings at all (see
    Ledger.record_recon_findings), so a payment with nothing here has either
    never been reconciled or tied out cleanly, and this cannot tell those
    two apart - which is fine, because both answers are "nothing to flag".
    """
    from merchant.ledger import Ledger

    led = Ledger(DB, business_id)
    try:
        latest = led.conn.execute(
            "SELECT run_id FROM business_recon_runs WHERE business_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (business_id,)).fetchone()
        if not latest:
            return []
        rows = led.conn.execute(
            "SELECT run_id, finding, action, at_stake, reasoning, detail"
            " FROM recon_findings WHERE business_id = ? AND run_id = ?"
            " AND txn_id = ? ORDER BY created_at DESC",
            (business_id, latest["run_id"], payment_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        led.close()


def build_tools(business_id: str, found: Optional[list] = None,
                credit_found: Optional[list] = None,
                recon_found: Optional[list] = None) -> list[Callable]:
    """
    Three tools, offered only when there is a real business to check against.

    `found`, `credit_found` and `recon_found` are how the caller gets back
    what these tools actually turned up, without threading a new return
    value through judge()'s whole call chain: pass a list, and whatever a
    tool surfaces during this run gets appended to it as the tool is called.
    The caller reads it back after judge() returns and attaches it to the
    verdict, so the merchant sees a link to the actual finding - not just a
    sentence naming it in the agent's prose.
    """
    if not business_id:
        return []

    @beta_tool
    def settlement_status(payment_id: str) -> str:
        """Check this platform's OWN settlement audit for a payment before
        counting on the money it is worth.

        A receipt being "already earned" only means the sale happened. It
        says nothing about whether Razorpay's settlement of that exact
        payment matches what the merchant is actually owed - this
        platform's settlement auditor checks exactly that, separately, and
        this is the one place the two connect. Use it on the reference of
        any receipt you are relying on to justify holding a payout, before
        you rely on it.

        Args:
            payment_id: The reference shown against a receipt in the
                evidence, exactly as given (e.g. "pay_9K3fL2xQ1z").
        """
        try:
            findings = _lookup(business_id, payment_id)
        except Exception as exc:                            # noqa: BLE001
            return json.dumps({
                "error": f"could not check: {type(exc).__name__}",
                "payment_id": payment_id})

        if not findings:
            return json.dumps({
                "payment_id": payment_id, "audited": False,
                "note": "no settlement audit has looked at this payment yet"})

        open_disputes = [f for f in findings
                         if f["action"] in ("dispute", "escalate")
                         and not f["human_reviewed"]]
        at_risk = sum(f["money_at_stake"] or 0 for f in open_disputes)

        if found is not None:
            for f in open_disputes:
                found.append({
                    "payment_id": payment_id, "run_id": f["run_id"],
                    "exception_code": f["exception_code"],
                    "money_at_stake": f["money_at_stake"] or 0,
                    "money_at_stake_display": _money(f["money_at_stake"] or 0)["display"],
                })

        return json.dumps({
            "payment_id": payment_id, "audited": True,
            "findings": [{
                "exception_code": f["exception_code"], "action": f["action"],
                "money_at_stake": _money(f["money_at_stake"] or 0),
                "reasoning": f["reasoning"], "rule_cited": f["rule_cited"],
                "resolved": bool(f["human_reviewed"]),
            } for f in findings],
            "has_unresolved_dispute": bool(open_disputes),
            "at_risk": _money(at_risk),
        })

    @beta_tool
    def at_risk_input_credit() -> str:
        """Check whether this business's GST input-credit reconciler found
        claimed tax credit that should not have been claimed.

        This is not about any one receipt or payout - it is a standing
        liability sitting outside the thirty-day curve entirely. Credit that
        is blocked, past its claim deadline, sitting on a supplier unpaid
        past 180 days, or claimed twice becomes a demand from the tax
        department if nobody reverses it, with interest at 18% a year under
        s.50. Call this once, before deciding the month looks comfortable -
        a balance that is fine on paper is not fine if part of it is
        already owed back.
        """
        try:
            rows = _overclaimed(business_id)
        except Exception as exc:                            # noqa: BLE001
            return json.dumps({"error": f"could not check: {type(exc).__name__}"})

        if not rows:
            return json.dumps({
                "checked": True, "at_risk_paise": 0,
                "note": "no claimed credit on file needs to be reversed"})

        at_risk = sum(r["money_at_stake"] or 0 for r in rows)
        if credit_found is not None:
            credit_found.append({
                "at_risk_paise": at_risk,
                "at_risk_display": _money(at_risk)["display"],
                "count": len(rows),
            })

        return json.dumps({
            "checked": True, "at_risk_paise": at_risk,
            "at_risk_display": _money(at_risk)["display"],
            "claims": [{
                "supplier": r["supplier_name"], "invoice": r["invoice_number"],
                "reason": r["exception_code"],
                "amount": _money(r["money_at_stake"] or 0),
                "claim_deadline": r["claim_deadline"],
            } for r in rows[:5]],
        })

    @beta_tool
    def recon_status(payment_id: str) -> str:
        """Check whether this platform's three-way reconciler already
        flagged a problem with this exact payment.

        The reconciler matches your invoice, the gateway's settlement line,
        and the bank credit for the same sale - a different question from
        whether the FEE was correct. If it found the settlement was never
        credited, or the bank credited a different amount, that is stronger
        evidence the money is not arriving than a rate check can see: it is
        not a dispute over how much, it is money that left one party and
        did not reach the other.

        Args:
            payment_id: The reference shown against a receipt in the
                evidence, exactly as given (e.g. "pay_9K3fL2xQ1z").
        """
        try:
            findings = _recon_lookup(business_id, payment_id)
        except Exception as exc:                            # noqa: BLE001
            return json.dumps({
                "error": f"could not check: {type(exc).__name__}",
                "payment_id": payment_id})

        if not findings:
            return json.dumps({
                "payment_id": payment_id, "checked": True, "flagged": False,
                "note": "no reconciliation exception on file for this "
                        "payment"})

        at_risk = sum(f["at_stake"] or 0 for f in findings)
        if recon_found is not None:
            for f in findings:
                recon_found.append({
                    "payment_id": payment_id, "run_id": f["run_id"],
                    "finding": f["finding"], "at_stake": f["at_stake"] or 0,
                    "at_stake_display": _money(f["at_stake"] or 0)["display"],
                })

        return json.dumps({
            "payment_id": payment_id, "checked": True, "flagged": True,
            "findings": [{
                "finding": f["finding"], "action": f["action"],
                "at_stake": _money(f["at_stake"] or 0),
                "reasoning": f["reasoning"], "detail": f["detail"],
            } for f in findings],
            "at_risk": _money(at_risk),
        })

    return [settlement_status, at_risk_input_credit, recon_status]
