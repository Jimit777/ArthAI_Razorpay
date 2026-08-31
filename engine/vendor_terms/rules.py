"""
The vendor-terms rules. Pure Python, no model, never wrong.

## What's reused, and why

`Tolerance` and `rupees` come from `engine.gst.rules` - the same tolerance
band (Rs 1 or 0.5%, whichever is larger) and Indian-digit-grouping display
this codebase has already built once, not reinvented here. Same reuse
`engine/gst_filing/rules.py` already does for the same two names.

There is no statutory source for a supplier's negotiated price - it is
whatever the merchant agreed, which is why this module has no citation
seam of its own: the only "rule" here is arithmetic on a number the
merchant themselves entered.
"""

from __future__ import annotations

import re

from engine.gst.rules import Tolerance, rupees

__all__ = ["Tolerance", "rupees", "normalise_item_key"]

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def normalise_item_key(description: str) -> str:
    """
    The join key between a billed line item and a rate-card row.

    Lower-cased, punctuation stripped, whitespace collapsed - so "Steel
    Rod - 12mm" and "steel rod 12mm" join, but two genuinely different
    items never silently collide because nothing here abbreviates or
    guesses at a match beyond this normalisation. Used identically at
    import time and at rate-card-entry time so the two sides actually meet.
    """
    text = _PUNCTUATION.sub(" ", (description or "").strip().lower())
    return _WHITESPACE.sub(" ", text).strip()
