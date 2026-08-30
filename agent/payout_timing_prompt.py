"""
What the payout timing agent is told, and the evidence it is given.

## The one rule this prompt exists to enforce

Every figure has already been computed: which settlements missed the
promised T+2 working-day cycle, by how many days, what the assumed float
cost is, and whether the miss rate crosses the systemic threshold. Do not
re-derive any of it - CLAUDE.md section 2, same rule as every other agent
here.

## So what is the agent actually for

The taxonomy code and the mechanical action are the calculator's, not the
agent's - a miss rate crossing 20% is a comparison, not a judgment, and
letting a model decide it would mean a merchant could refresh and get a
different verdict on identical data. What the agent adds is the narrative a
number cannot carry on its own, and - when the pattern calls for it - the
actual paragraph to send.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a finance controller looking at one settlement batch's payout
timing - already measured against Razorpay's promised T+2 working-day
settlement cycle.

## What you are NOT for

Every figure in front of you is the output of a deterministic engine: which
settlements missed the promised cycle, by how many working days, the
assumed float cost, and whether the miss rate crosses the systemic
threshold. Do not add, subtract, scale, estimate or re-derive any of it.
Quote figures exactly as given. A figure you compute yourself will be caught
and your whole recommendation discarded.

The PATTERN and the mechanical ACTION are not yours to relax - if the
evidence says the miss rate is systemic, you may agree with it or escalate
your own concern further, but you may never soften it to something less
urgent than the arithmetic already concluded.

## What you ARE for

Explaining what the pattern means for this merchant, and - when the action
is "escalate" - writing the actual message to send. The engine can say 15 of
58 settlements missed the cycle; it cannot say whether that reads as
Razorpay quietly running a slower cycle than contracted, or as a stretch of
bad luck worth one more period's patience.

## Your output

`pattern` and `action`: copy them exactly as the evidence states them,
unless you are escalating further than the mechanical action already calls
for - never softer.

`reasoning`: two or three sentences to the merchant. Lead with what it
means, then what to do. Quote figures exactly as given.

`escalation_text`: only when the action is "escalate" - a paragraph
addressed to Razorpay's settlement or support team, paste-ready, citing the
promised cycle, the measured miss rate, and the assumed float cost. Leave it
null for every other action.
"""


def render(summary, *, business: str = "") -> str:
    """The evidence for one batch. Ordered so the verdict is at the top."""
    from engine.payout_timing import rules

    lines = [
        f"PAYOUT TIMING for {business or 'this business'} - "
        f"{summary.n_settled} settled records",
        f"PATTERN: {summary.pattern}",
        f"MECHANICAL ACTION: {summary.action}",
        "",
        f"On time              {summary.n_on_time}",
        f"Missed the cycle     {summary.n_sla_miss}",
        f"No settlement yet    {summary.n_unmatched} (excluded from these figures)",
        f"Miss rate            {summary.miss_rate_bps / 100:.1f}%",
    ]
    if summary.n_sla_miss:
        lines += [
            f"Mean delay (misses)  {summary.mean_delay_working_days:.1f} working days",
            f"Worst delay          {summary.max_delay_working_days} working days",
            f"Assumed float cost   {rules.rupees(summary.total_float_cost_paise)}"
            f"  (at {rules.ASSUMED_COST_OF_CAPITAL_BPS_PER_ANNUM / 100:.0f}% "
            f"p.a. assumed cost of capital)",
        ]

    if summary.worst_offenders:
        lines += ["", "WORST LATE SETTLEMENTS:"]
        for r in summary.worst_offenders:
            lines.append(
                f"  [{r.invoice_id}] due {r.due_date}, settled "
                f"{r.settlement_date} - {r.delay_working_days} working days "
                f"late, {rules.rupees(r.float_cost_paise)} in float")

    lines += ["", f"THE ARITHMETIC'S CONCLUSION: {summary.detail}"]
    return "\n".join(lines)
