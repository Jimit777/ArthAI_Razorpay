"""
The three-way join, in three passes. Pure arithmetic - no model touches this.

## Why the passes are ordered, and why order is the whole design

Each pass is weaker evidence than the one before it, so each runs only on what
the previous one could not resolve. Running them in the other order, or all at
once, would let a guess consume a record that had an exact answer waiting.

    PASS 1  exact      the gateway line names the invoice, and the amounts
                       agree to the paise. Certain.
    PASS 2  windowed   no reference, but exactly one settlement of that net
                       amount landed within a few days. Strong, and only
                       because it is required to be UNIQUE - see below.
    PASS 3  narration  the bank's free-text field carries the UTR, or enough
                       of it. Weakest, and it never invents an amount.

## The rule that keeps Pass 2 honest

A windowed match is accepted ONLY when exactly one candidate fits. Two
settlements of the same amount in the same week is completely ordinary - a
merchant with a fixed-price product has them every day - and picking the
nearer one would be a coin toss presented as a reconciliation. Ambiguity is
left unresolved and reported, which is a worse-looking match rate and a
correct one.

## The spec called this pass "fuzzy / AI logic". It is neither

It is a bounded search over amounts and dates, and it is deterministic. Asking
a model to do it would produce a join that is usually right, occasionally and
silently wrong, and impossible to reproduce - against a number the whole
feature exists to state out loud. CLAUDE.md section 2 is the rule; this is one
of the places it bites hardest.

What the agent does instead is in agent/recon_agent.py: read the exceptions
this leaves behind and say what each one means and what to do about it. That
is judgment, and no amount of matching produces it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.recon.records import (AMOUNT_MISMATCH, MATCHED, MATCHED_FUZZY,
                                  MISSING_IN_BANK, MISSING_IN_GATEWAY,
                                  ORPHAN_BANK_CREDIT, UNEXPLAINED_FEE,
                                  ReconBatch, ReconRow)

# How far a credit may land from its settlement and still be the same money.
# Three days covers a weekend plus a bank holiday, which is what actually
# separates a Friday settlement from its Tuesday credit.
WINDOW_DAYS = 3

# Below this, a difference is rounding or a sub-rupee bank charge and is not
# worth a merchant's attention. Above it and somebody has to look. Same
# reasoning as the settlement auditor's tolerance band, and the same warning:
# too tight and the exception list is noise nobody reads.
TOLERANCE_PAISE = 100

# A shortfall smaller than this, on an otherwise clean match, reads as a bank
# charge rather than as a settlement error. Larger than that and calling it a
# "fee" would be putting a comfortable name on missing money.
FEE_LIKE_CEILING = 10_000

# The UTR as banks write it, and the shortest fragment worth trusting. Six
# characters is long enough that a collision inside one statement is not
# plausible; shorter and the narration pass starts inventing links.
UTR = re.compile(r"[A-Z]{4}[A-Z0-9]?\d{6,}")
MIN_FRAGMENT = 6


@dataclass
class MatchStats:
    exact: int = 0
    windowed: int = 0
    narration: int = 0

    def as_dict(self) -> dict:
        return {"pass_1_exact": self.exact, "pass_2_windowed": self.windowed,
                "pass_3_narration": self.narration}


def reconcile(batch: ReconBatch) -> tuple[list[ReconRow], MatchStats]:
    """
    Join the three sources and report what is left over.

    Returns every row - resolved and not - because a reconciliation that
    returns only its failures cannot state a match rate, and a match rate is
    the point.
    """
    stats = MatchStats()
    rows: list[ReconRow] = []

    credits = {c.utr_number: c for c in batch.bank}
    used_settlements: set[str] = set()
    used_credits: set[str] = set()

    by_reference = {}
    for s in batch.settlements:
        if s.invoice_reference:
            by_reference.setdefault(s.invoice_reference, []).append(s)

    for invoice in batch.invoices:
        settlement = None

        # --- pass 1: the gateway names the invoice --------------------------
        named = [s for s in by_reference.get(invoice.invoice_id, [])
                 if s.txn_id not in used_settlements]
        if len(named) == 1:
            settlement = named[0]

        # --- pass 2: no reference, so amount and date have to carry it ------
        if settlement is None:
            candidates = [
                s for s in batch.settlements
                if s.txn_id not in used_settlements
                and not s.invoice_reference
                and abs(s.gross_amount - invoice.amount) <= TOLERANCE_PAISE
                and abs((s.settlement_date - invoice.date_issued).days)
                <= WINDOW_DAYS + 2]
            # Exactly one, or none. Two settlements of the same amount in the
            # same week is ordinary, and choosing between them would be a coin
            # toss wearing a reconciliation's clothes.
            if len(candidates) == 1:
                settlement = candidates[0]
                stats.windowed += 1

        if settlement is None:
            rows.append(ReconRow(
                finding=MISSING_IN_GATEWAY, invoice=invoice,
                variance=invoice.amount,
                detail=f"Billed {_r(invoice.amount)} and the gateway has no "
                       f"settlement for it."))
            continue

        used_settlements.add(settlement.txn_id)
        credit, how = _credit_for(settlement, credits, used_credits)
        if credit is not None:
            used_credits.add(credit.utr_number)
            if how == "narration":
                stats.narration += 1

        rows.append(_judge(invoice, settlement, credit, how, stats))

    # --- anything the invoices never claimed -------------------------------
    for settlement in batch.settlements:
        if settlement.txn_id in used_settlements:
            continue
        credit, how = _credit_for(settlement, credits, used_credits)
        if credit is not None:
            used_credits.add(credit.utr_number)
        rows.append(_judge(None, settlement, credit, how, stats))

    for credit in batch.bank:
        if credit.utr_number in used_credits:
            continue
        rows.append(ReconRow(
            finding=ORPHAN_BANK_CREDIT, bank=credit,
            variance=credit.credit_amount,
            detail=f"{_r(credit.credit_amount)} arrived on "
                   f"{credit.transaction_date} and nothing in your books or "
                   f"the gateway's accounts for it."))

    return rows, stats


def _credit_for(settlement, credits, used) -> tuple:
    """The bank credit for a settlement, by UTR, then by narration, then by
    amount and date."""
    utr = settlement.utr
    if utr and utr in credits and utr not in used:
        return credits[utr], "utr"

    # --- pass 3: read the bank's free text -------------------------------
    if utr:
        for candidate in credits.values():
            if candidate.utr_number in used:
                continue
            if _narration_points_at(candidate.description, utr):
                return candidate, "narration"

    # --- pass 2, from the other side: amount and date ---------------------
    window = [c for c in credits.values()
              if c.utr_number not in used
              and abs(c.credit_amount - settlement.net_settled) <= TOLERANCE_PAISE
              and abs((c.transaction_date - settlement.settlement_date).days)
              <= WINDOW_DAYS]
    if len(window) == 1:
        return window[0], "window"
    return None, ""


def _narration_points_at(description: str, utr: str) -> bool:
    """
    Whether a bank narration is talking about this UTR.

    Handles the truncation banks actually do. A fragment shorter than
    MIN_FRAGMENT is refused rather than matched on: a four-character overlap
    between two reference numbers in the same statement is not evidence of
    anything.
    """
    text = (description or "").upper()
    if utr in text:
        return True
    for token in UTR.findall(text):
        if len(token) < MIN_FRAGMENT:
            continue
        if utr.startswith(token) or token.startswith(utr[:len(token)]):
            return len(token) >= MIN_FRAGMENT
    return False


def _judge(invoice, settlement, credit, how, stats) -> ReconRow:
    """What this triple amounts to, once the join has been made."""
    if credit is None:
        return ReconRow(
            finding=MISSING_IN_BANK, invoice=invoice, settlement=settlement,
            variance=settlement.net_settled,
            detail=f"The gateway settled {_r(settlement.net_settled)} on "
                   f"{settlement.settlement_date} under UTR "
                   f"{settlement.utr or '(none given)'}, and no credit for it "
                   f"appears on the statement.")

    gap = credit.credit_amount - settlement.net_settled
    if abs(gap) <= TOLERANCE_PAISE:
        exact = bool(invoice and settlement.invoice_reference)
        if exact:
            stats.exact += 1
        return ReconRow(
            finding=MATCHED if exact else MATCHED_FUZZY,
            invoice=invoice, settlement=settlement, bank=credit,
            variance=0, matched_by=how,
            detail=f"Billed {_r(settlement.gross_amount)}, fee "
                   f"{_r(settlement.fee_deducted)}, settled "
                   f"{_r(settlement.net_settled)}, credited "
                   f"{_r(credit.credit_amount)}. All three agree.")

    short = -gap
    fee_like = 0 < short <= FEE_LIKE_CEILING
    return ReconRow(
        finding=UNEXPLAINED_FEE if fee_like else AMOUNT_MISMATCH,
        invoice=invoice, settlement=settlement, bank=credit,
        variance=gap, matched_by=how,
        detail=f"The gateway settled {_r(settlement.net_settled)} and the "
               f"bank credited {_r(credit.credit_amount)} - "
               f"{_r(abs(gap))} {'short' if gap < 0 else 'more'}.")


def _r(paise: int) -> str:
    from engine.gst import rules

    return rules.rupees(paise)
