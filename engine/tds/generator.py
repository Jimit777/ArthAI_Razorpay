"""
Synthetic TDS deductions and credit-statement lines, with known errors planted.

Same trick as every other generator in this project (CLAUDE.md section 7):
plant the errors, hand back the answer key, and the demo becomes a
measurement.

## What a real reconciliation would join, and the simplification made here

    deduction     what Razorpay's settlement report says it withheld
    credit        what the merchant's own Form 26AS / Form 168 shows

The join key here is `payment_id`. A REAL Form 26AS or Form 168 carries no
such reference - it aggregates by deductor TAN, deductee PAN, section/code
and quarter, with nothing that maps back to a single `pay_XXXX`. Keeping
`payment_id` on the synthetic credit side is a deliberate simplification, in
the same spirit as CLAUDE.md section 7.1's "hybrid" data strategy: it is what
makes an exact, measurable demo possible in the time available. A future
Upload/Connected mode reconciling real statements would need amount+date
fuzzy matching instead of an exact join - noted here rather than pretended
away.

The planted errors straddle 1 April 2026 on purpose: the whole point of this
agent is showing the regime transition (194O/1%/Form 26AS becoming
1035/0.1%/Form 168) get handled correctly on both sides of the date.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from engine.tds.rules import (NEW_CODE, NEW_FORM, NEW_RATE_BPS, OLD_CODE,
                              OLD_FORM, OLD_RATE_BPS, REGIME_CHANGE,
                              Tolerance, expected_form,
                              expected_section_code, quarter_of)
from engine.tds.taxonomy import TdsCode

# A batch is "as of" a fixed date for reproducibility - matches the settlement
# and ITC generators' convention of pinning "today" rather than using the
# real clock.
AS_OF = date(2026, 8, 24)

_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _rzp_id(rng: random.Random, prefix: str, length: int = 14) -> str:
    """Razorpay-shaped id: a prefix plus base62 characters, same convention
    as generator/synthetic.py's own `_rzp_id` - written locally rather than
    imported so the engine layer does not depend on the generator layer."""
    return prefix + "".join(rng.choice(_ID_ALPHABET) for _ in range(length))


@dataclass
class Deduction:
    """One TDS line as Razorpay's own settlement report shows it."""
    payment_id: str
    gross_amount: int               # paise, the payout TDS was computed on
    section_code: str               # what Razorpay's report actually labelled it
    rate_bps: int                   # rate Razorpay actually applied
    amount: int                     # paise actually withheld
    deducted_at: date


@dataclass
class CreditEntry:
    """One line on the merchant's own government tax-credit statement."""
    payment_id: str
    form: str                       # 'Form 26AS' | 'Form 168'
    code_shown: str
    amount: int                     # paise
    credited_period: str            # FY quarter, e.g. 'FY2026-27 Q1'
    posted_at: date


@dataclass
class TdsBatch:
    deductions: list[Deduction]
    credits: list[CreditEntry]
    as_of: date = AS_OF
    tolerance: Tolerance = field(default_factory=Tolerance)


# n=60, split across the regime boundary so the demo exercises both eras.
CANONICAL_MIX: dict[str, int] = {
    "clean": 41,
    "rounding": 4,                  # decoy - under tolerance, must not flag
    "rate_mismatch": 4,
    "code_mismatch": 4,
    "missing_credit": 4,
    "period_mismatch": 3,
}

RECIPE_TRUTH: dict[str, TdsCode] = {
    "clean": TdsCode.CREDIT_CLEAN,
    "rounding": TdsCode.ROUNDING,
    "rate_mismatch": TdsCode.RATE_MISMATCH,
    "code_mismatch": TdsCode.CODE_MISMATCH,
    "missing_credit": TdsCode.MISSING_CREDIT,
    "period_mismatch": TdsCode.PERIOD_MISMATCH,
}

DECOY_RECIPES = {"rounding"}
CLEAN_RECIPES = {"clean"}


def generate_batch(n: int = 60, seed: int = 20260905
                   ) -> tuple[TdsBatch, dict[str, str]]:
    """
    Returns (batch, ground_truth) where ground_truth maps payment_id to the
    TdsCode that should be concluded about it.
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)

    deductions: list[Deduction] = []
    credits: list[CreditEntry] = []
    truth: dict[str, str] = {}

    for i, recipe in enumerate(recipes):
        # Alternate pre/post-change so every recipe, clean included, is
        # exercised on both sides of 1 April 2026 - the transition itself is
        # the thing this agent has to get right.
        post_change = i % 2 == 0
        payment_id = _rzp_id(rng, "pay_")
        _build(recipe, payment_id, post_change, rng, deductions, credits, truth)

    rng.shuffle(credits)
    return TdsBatch(deductions=deductions, credits=credits), truth


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 60:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            scaled = max(1, round(count * n / 60)) if recipe != "clean" else 0
            recipes += [recipe] * scaled
        recipes += ["clean"] * max(0, n - len(recipes))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes


def _random_date(post_change: bool, rng: random.Random) -> date:
    if post_change:
        span = (AS_OF - REGIME_CHANGE).days
        return REGIME_CHANGE + timedelta(days=rng.randint(0, max(0, span - 5)))
    start = REGIME_CHANGE - timedelta(days=150)
    return start + timedelta(days=rng.randint(0, 130))


def _build(recipe, payment_id, post_change, rng, deductions, credits, truth
          ) -> None:
    deducted_at = _random_date(post_change, rng)
    gross = rng.randrange(20_000_00, 500_000_00, 100)
    correct_rate = NEW_RATE_BPS if deducted_at >= REGIME_CHANGE else OLD_RATE_BPS
    correct_code = expected_section_code(deducted_at)
    correct_form = expected_form(deducted_at)
    amount = (gross * correct_rate + 5_000) // 10_000

    deduction = Deduction(
        payment_id=payment_id, gross_amount=gross, section_code=correct_code,
        rate_bps=correct_rate, amount=amount, deducted_at=deducted_at)
    deductions.append(deduction)
    truth[payment_id] = str(RECIPE_TRUTH[recipe])

    quarter = quarter_of(deducted_at)
    posted_at = deducted_at + timedelta(days=rng.randint(20, 40))

    if recipe == "missing_credit":
        return                      # nothing on the statement at all

    if recipe == "rate_mismatch":
        # The statement implies the OTHER era's rate - the 1%-vs-0.1%
        # straddle error this agent exists to catch.
        wrong_rate = OLD_RATE_BPS if correct_rate == NEW_RATE_BPS else NEW_RATE_BPS
        credits.append(CreditEntry(
            payment_id=payment_id, form=correct_form, code_shown=correct_code,
            amount=(gross * wrong_rate + 5_000) // 10_000,
            credited_period=quarter, posted_at=posted_at))
        return

    if recipe == "code_mismatch":
        # The statement quotes the OTHER era's code/form for this date -
        # a stale 194O/26AS after the change, or a premature 1035/168 before it.
        wrong_code = OLD_CODE if correct_code == NEW_CODE else NEW_CODE
        wrong_form = OLD_FORM if correct_form == NEW_FORM else NEW_FORM
        credits.append(CreditEntry(
            payment_id=payment_id, form=wrong_form, code_shown=wrong_code,
            amount=amount, credited_period=quarter, posted_at=posted_at))
        return

    if recipe == "rounding":
        nudge = max(1, Tolerance().band(amount) // 2)
        credits.append(CreditEntry(
            payment_id=payment_id, form=correct_form, code_shown=correct_code,
            amount=amount - nudge, credited_period=quarter,
            posted_at=posted_at))
        return

    if recipe == "period_mismatch":
        # Posted a quarter later than the deduction's own quarter - the
        # statement's ordinary refresh lag crossing a boundary.
        later = deducted_at + timedelta(days=95)
        credits.append(CreditEntry(
            payment_id=payment_id, form=correct_form, code_shown=correct_code,
            amount=amount, credited_period=quarter_of(later),
            posted_at=later + timedelta(days=25)))
        return

    # clean
    credits.append(CreditEntry(
        payment_id=payment_id, form=correct_form, code_shown=correct_code,
        amount=amount, credited_period=quarter, posted_at=posted_at))
