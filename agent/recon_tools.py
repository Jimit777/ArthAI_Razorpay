"""
The three-way reconciler's tools. Two of them, both read-only, both searches.

## What they are for

The matcher runs a strict, narrow search: exact reference, or exactly one
candidate within a three-day window and a hundred-paise tolerance. That
strictness is deliberate - CLAUDE.md's rule against guessing between
candidates - and it means a genuine match sometimes sits just outside the
band the matcher was told to trust automatically.

These tools let the agent widen the search on ONE exception, the way a person
investigating it would: look further out, see what is plausible, and say so.
They do not change what counts as matched. The matcher's output is still the
only thing that decides the match rate; these are a place for the agent to
look before it recommends `investigate` rather than guessing blind.

## Read-only by construction

Same rule as every other tool file here. There is no claim, no link, no write
- a search that returns a list of possibilities, priced and dated, and
nothing that could act on what it finds.
"""

from __future__ import annotations

import json
from typing import Callable

from anthropic import beta_tool

from engine.gst import rules

# How far outside the matcher's own bands these tools are willing to look.
# Wider than WINDOW_DAYS on purpose - if the honest answer were "nothing
# within two weeks either", that is worth knowing too.
SEARCH_WINDOW_DAYS = 14


def _money(paise: int) -> dict:
    return {"paise": paise, "display": rules.rupees(paise)}


def build_tools(pool: list, exclude=None) -> list[Callable]:
    """
    Build the tool set bound to one reconciliation's leftover pool.

    `pool` is every OTHER exception row's settlement or bank credit - the
    money nothing has claimed yet. Closed over per run, so two reconciliations
    never see each other's unclaimed money.
    """
    settlements = [r.settlement for r in pool
                  if r.settlement and r is not exclude]
    credits = [r.bank for r in pool if r.bank and r is not exclude]

    @beta_tool
    def nearby_settlements(around_amount_paise: int, around_date: str,
                           window_days: int = SEARCH_WINDOW_DAYS) -> str:
        """Search every gateway settlement that nothing has claimed yet, for
        ones close to a given amount and date. Use this when a bank credit or
        an invoice has no settlement matched to it, to see whether something
        plausible exists just outside the automatic window.

        Args:
            around_amount_paise: The amount to search near, in paise (rupees
                times 100).
            around_date: The date to search near, as YYYY-MM-DD.
            window_days: How many days either side of the date to search.
        """
        from datetime import date as _date

        try:
            centre = _date.fromisoformat(around_date)
        except ValueError:
            return json.dumps({"error": f"{around_date} is not YYYY-MM-DD"})

        found = []
        for s in settlements:
            day_gap = abs((s.settlement_date - centre).days)
            if day_gap > window_days:
                continue
            found.append({
                "txn_id": s.txn_id, "gross_amount": _money(s.gross_amount),
                "net_settled": _money(s.net_settled),
                "settlement_date": str(s.settlement_date),
                "days_away": day_gap,
                "amount_gap": _money(abs(s.gross_amount - around_amount_paise)),
                "has_reference": bool(s.invoice_reference),
                "utr": s.utr})
        found.sort(key=lambda f: (f["amount_gap"]["paise"], f["days_away"]))
        return json.dumps({"count": len(found), "results": found[:6]})

    @beta_tool
    def nearby_bank_credits(around_amount_paise: int, around_date: str,
                            window_days: int = SEARCH_WINDOW_DAYS) -> str:
        """Search every bank credit that nothing has claimed yet, for ones
        close to a given amount and date. Use this when a settlement has no
        credit matched to it, to see whether something plausible arrived just
        outside the automatic window.

        Args:
            around_amount_paise: The amount to search near, in paise.
            around_date: The date to search near, as YYYY-MM-DD.
            window_days: How many days either side of the date to search.
        """
        from datetime import date as _date

        try:
            centre = _date.fromisoformat(around_date)
        except ValueError:
            return json.dumps({"error": f"{around_date} is not YYYY-MM-DD"})

        found = []
        for c in credits:
            day_gap = abs((c.transaction_date - centre).days)
            if day_gap > window_days:
                continue
            found.append({
                "utr_number": c.utr_number,
                "credit_amount": _money(c.credit_amount),
                "transaction_date": str(c.transaction_date),
                "days_away": day_gap,
                "amount_gap": _money(abs(c.credit_amount - around_amount_paise)),
                "description": c.description})
        found.sort(key=lambda f: (f["amount_gap"]["paise"], f["days_away"]))
        return json.dumps({"count": len(found), "results": found[:6]})

    return [nearby_settlements, nearby_bank_credits]
