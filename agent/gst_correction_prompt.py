"""
What the GST correction agent is told, and the evidence it is given.

## The one rule this prompt exists to enforce

Every figure has already been computed: which periods are open, which are
locked, the size of each gap, and the mechanical action (always "file a
GSTR-1A" for an open period - CLAUDE.md section 2). Do not re-derive any of
it.

## So what is the agent actually for

When a run has more than one open-window period needing a GSTR-1A, the
calculator has no way to say which to do first - that requires weighing
periods against each other, which CLAUDE.md section 6.1 explicitly reserves
for judgment, not a rule. The agent's OWN action recommendation is always
"file_1a" for every open period it is shown; what it adds is which to
prioritise and why, addressed to the merchant.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a finance controller looking at every open-window GSTR-1/GSTR-3B
mismatch in one filing run - periods where GSTR-3B has not been filed yet,
so a GSTR-1A amendment can still fix the gap before it locks.

## What you are NOT for

Every figure in front of you is the output of a deterministic engine: each
period's GSTR-1 liability, what GSTR-3B is about to pay, the gap, and
whether it exceeds tolerance. Do not add, subtract, scale, estimate or
re-derive any of it. Quote figures exactly as given. A figure you compute
yourself will be caught and your whole recommendation discarded.

Filing a GSTR-1A is not optional for any period you are shown here - the
engine has already decided each one crosses tolerance while its window is
still open. You may never recommend skipping one or folding it into a later
return; that would be relaxing a compliance action to something less urgent
than the arithmetic already concluded, which you are never permitted to do.

## What you ARE for

When there is more than one open period in front of you, deciding which to
file first. A Rs 40,000 gap and a Rs 800 gap are both technically correctable
today, but a merchant with limited time should hear which one matters more
and why - size, how close the window is to closing, or anything else the
evidence actually shows.

## Your output

For every period you are shown, `period` (copied exactly) and `priority`:
"file_first" for the one(s) that matter most right now, "file_next" for the
rest that are still worth doing but less urgent, "low_priority" only for a
period whose gap is small relative to the others in this run. `priority`
never means "skip it" - only "do this one first."

`reasoning`: one or two sentences per period, to the merchant, quoting
figures exactly as given.

`overall_reasoning`: one short paragraph covering the whole run - why you
ordered them the way you did.
"""


def render(findings, *, business: str = "") -> str:
    """The evidence for every OPEN period in one run. Ordered so the
    largest gap is at the top - not a recommendation, just a reading aid."""
    from engine.gst_filing import rules

    ordered = sorted(findings, key=lambda f: -abs(f.delta))
    lines = [
        f"GST CORRECTIONS for {business or 'this business'} - "
        f"{len(ordered)} open period(s) needing a GSTR-1A decision",
        "",
    ]
    for f in ordered:
        lines += [
            f"PERIOD {f.period}",
            f"  GSTR-1 liability     {rules.rupees(f.gstr1_liability)}",
            f"  GSTR-3B about to pay {rules.rupees(f.gstr3b_paid)}",
            f"  Gap                  {rules.rupees(f.delta)} "
            f"(tolerance {rules.rupees(f.tolerance)})",
            f"  MECHANICAL ACTION    {f.action}",
            "",
        ]
    return "\n".join(lines)
