"""
The TDS credit rules. Pure Python, no model, never wrong.

Same architectural rule as every other engine in this project (CLAUDE.md
section 2): the calculator decides what the law says for a given date. The
agent decides what a gap MEANS.

## The one fact this module exists to get right

| # | Rule | Source |
|---|---|---|
| 1 | Before 1 Apr 2026: TDS at 1% under section 194O, reported in Form 26AS | Income Tax Act 1961 s.194O |
| 2 | From 1 Apr 2026: TDS at 0.1% under s.393(1) Sl.8(v), code 1035, reported in Form 168 | Income Tax Act 2025 |

These are the same constants already proven in `generator/synthetic.py`
(`TDS_OLD_BPS`, `TDS_NEW_BPS`, `TDS_REGIME_CHANGE`) and `agent/tools.py`'s
`tds_code_map` - defined once more here because the engine layer cannot
import from either without an inverted or agent-layer dependency, not
because the facts differ.

ALL MONEY IN PAISE, AS INTEGERS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

REGIME_CHANGE = date(2026, 4, 1)

OLD_CODE, NEW_CODE = "194O", "1035"
OLD_RATE_BPS, NEW_RATE_BPS = 100, 10            # 1% pre, 0.1% post
OLD_FORM, NEW_FORM = "Form 26AS", "Form 168"
OLD_PROVISION = "Income Tax Act 1961 s.194O"
NEW_PROVISION = "Income Tax Act 2025 s.393(1) Sl. 8(v)"


def expected_rate_bps(deducted_on: date) -> int:
    return NEW_RATE_BPS if deducted_on >= REGIME_CHANGE else OLD_RATE_BPS


def expected_section_code(deducted_on: date) -> str:
    return NEW_CODE if deducted_on >= REGIME_CHANGE else OLD_CODE


def expected_form(deducted_on: date) -> str:
    return NEW_FORM if deducted_on >= REGIME_CHANGE else OLD_FORM


def expected_provision(deducted_on: date) -> str:
    return NEW_PROVISION if deducted_on >= REGIME_CHANGE else OLD_PROVISION


def quarter_of(when: date) -> str:
    """India's financial-year quarter, e.g. 'FY2026-27 Q1' for June 2026."""
    fy_start = when.year if when.month >= 4 else when.year - 1
    q = (when.month - 4) % 12 // 3 + 1
    return f"FY{fy_start}-{str(fy_start + 1)[-2:]} Q{q}"


@dataclass(frozen=True)
class Tolerance:
    """Below this, a difference is arithmetic noise rather than a finding.

    TDS lines run far smaller than a GST invoice's tax, so the floor here is
    half a rupee rather than the GST/settlement engines' one rupee - configured
    the same way (CLAUDE.md section 6.2: put it in config, never hard-code the
    judgment call it embodies) but tuned to this domain's amounts.
    """
    floor_paise: int = 50                       # Rs 0.50
    pct_bps: int = 50                            # 0.5%

    def band(self, amount_paise: int) -> int:
        return max(self.floor_paise, (abs(amount_paise) * self.pct_bps) // 10_000)


def rupees(paise: int) -> str:
    """Indian digit grouping. Rs 12,34,567.89, not Rs 1,234,567.89."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}Rs {s}.{frac:02d}"
