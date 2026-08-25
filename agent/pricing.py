"""
What a run cost, in one place.

The rates were inline in audit.py and nowhere else, so every other agent either
did not report a cost or would have had to repeat them - and three copies of a
price list is three things to forget when a price changes.

Rates are per million tokens, in US dollars, for the model this product runs
on. Cached input is a tenth of fresh input, which is why the prompts are
arranged so the expensive part is byte-identical across a batch.
"""

from __future__ import annotations

from dataclasses import dataclass

USD_PER_MILLION_INPUT = 5.0
USD_PER_MILLION_CACHED = 0.5
USD_PER_MILLION_OUTPUT = 25.0

# The Batches API is half price and slower. Not a live-demo trade.
BATCH_DISCOUNT = 0.5

# For showing a rupee figure beside the dollar one. Deliberately a round
# approximation and labelled as such - a live FX rate on a cost estimate is
# precision nobody asked for.
RUPEES_PER_USD = 88


@dataclass
class Usage:
    """Tokens across a whole run, and what they came to."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0
    batched: bool = False

    def add(self, verdict) -> "Usage":
        """Accumulate from anything carrying the usual token attributes."""
        self.input_tokens += getattr(verdict, "input_tokens", 0) or 0
        self.output_tokens += getattr(verdict, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(verdict, "cache_read_tokens", 0) or 0
        self.calls += 1
        return self

    @property
    def usd(self) -> float:
        rate = BATCH_DISCOUNT if self.batched else 1.0
        return rate * (
            self.input_tokens * USD_PER_MILLION_INPUT
            + self.cache_read_tokens * USD_PER_MILLION_CACHED
            + self.output_tokens * USD_PER_MILLION_OUTPUT) / 1_000_000

    @property
    def rupees(self) -> float:
        return self.usd * RUPEES_PER_USD

    @property
    def cached_share(self) -> float:
        """
        How much of the input came from cache.

        Worth showing: it is the difference between a batch costing rupees and
        costing hundreds of them, and it is a consequence of prompt design
        rather than luck.
        """
        total = self.input_tokens + self.cache_read_tokens
        return (self.cache_read_tokens / total) if total else 0.0

    def display(self) -> str:
        if not self.calls:
            return "no agent calls"
        return (f"{self.calls} call{'' if self.calls == 1 else 's'} &middot; "
                f"${self.usd:.3f} (about Rs {self.rupees:.0f})")

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cached_share_pct": round(self.cached_share * 100, 1),
            "usd": round(self.usd, 4),
            "rupees": round(self.rupees, 2),
        }
