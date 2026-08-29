"""
The supplier risk agent's tools. Two of them, both read-only, both drill-downs.

## What they are for

`render()` hands the agent a summary: totals over the whole history, and the
last twelve periods in full. That is enough to score a supplier and it is not
enough to answer "is this a one-off or a pattern" when the interesting months
sit further back, and it says nothing at all about the two statutory clocks
that decide whether THIS month's credit survives - Rule 37 and s.16(4) are
computed elsewhere in this codebase and were never handed to the agent doing
the judging.

These tools close both gaps. Neither widens what counts as risky - the
trust score, the pattern and the recommended action are still arithmetic,
computed before the agent is asked and never revisited by it. They widen what
the agent can look AT before it writes the sentence explaining why.

## Read-only by construction

Same rule as every other tool file here. Both return figures already
computed by engine/gst/risk.py; neither derives one.
"""

from __future__ import annotations

import json
from typing import Callable, Optional


def build_tools(history=None, clocks: Optional[dict] = None) -> list[Callable]:
    """
    Build the tool set bound to one supplier.

    Both `history` and `clocks` are optional and independent - a supplier
    with no invoices this month has clocks but nothing on them; a source that
    could not supply history still has this month's clocks. Only the tools
    for what is actually available are offered.
    """
    from anthropic import beta_tool

    tools = []

    if history is not None:
        rows = history.as_rows()

        @beta_tool
        def full_filing_history() -> str:
            """See this supplier's ENTIRE filing history, not just the last
            twelve periods you were given. Use this to check whether a
            default or a late run is a one-off from years back or a pattern
            that continues closer to now.
            """
            return json.dumps({
                "gstin": history.gstin, "total_periods": len(rows),
                "periods": rows})
        tools.append(full_filing_history)

    if clocks:
        @beta_tool
        def statutory_clocks() -> str:
            """See the Rule 37 (180-day payment window) and Section 16(4)
            (claim deadline) status for THIS supplier's invoices this month.
            Neither clock changes what to recommend - the recommendation is
            fixed by the figures - but a supplier who is fine on paper and
            three days from a Rule 37 breach is a more urgent phone call than
            one who is not, and you should say so if it is true.
            """
            return json.dumps(clocks)
        tools.append(statutory_clocks)

    return tools
