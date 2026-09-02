"""
What the agent is doing, narrated as it happens.

Guardrail 5 says every agent decision is timestamped and replayable. This turns
that trail into something a person can actually watch - and, crucially, the
same lines a person watched live are the lines they read afterwards.

## One set of builders, two sources of timing

The builders below are called from two places: by the runner while an audit is
in flight, and by `build()` reconstructing a finished one. That is deliberate.
If live narration and the replay were written separately they would drift, and
then the prettier one gets believed while the audited one does not.

What differs between the two is only WHEN the facts are known. The facts
themselves come from the same place either way - the variances and the audit
log - so a line cannot say anything the record does not contain.

## Voice

First person, plain language, and honest about uncertainty. The agent says what
it is about to do, what it found, and what it declined to conclude. It does not
narrate work it did not do: if it called no tools, no tool lines appear.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Optional

from engine.expected_value import rupees

# Line kinds. These drive colour and nothing else.
SAY = "say"        # context and observation, dim
DO = "do"          # something the agent is doing, green
THINK = "think"    # a judgement being formed, blue
FACT = "fact"      # a number that matters, bright
OK = "ok"          # a conclusion reached
TOOL = "tool"      # a tool call
NOTE = "note"      # something the reader should be careful about
FAIL = "fail"      # something went wrong


@dataclass
class Line:
    kind: str
    text: str
    at: str = ""
    offset: int = 0        # ms from the start of the run, for playback

    def as_dict(self) -> dict:
        return asdict(self)


def clock(ts: Optional[float] = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def line(kind: str, text: str, at: Optional[float] = None) -> Line:
    return Line(kind, text, clock(at))


# --- the narration -------------------------------------------------------
#
# One function per event the pipeline actually emits. Nothing here invents a
# step; if the code does not do it, there is no builder for it.

def opening(run_id: str, at=None) -> Line:
    return line(SAY, f"Opening settlement {run_id}.", at)


def loaded(payments: int, gross: int, credited: int, at=None) -> Line:
    return line(FACT, f"{payments} payment{'s' if payments != 1 else ''}, "
                      f"{rupees(gross)} sold, {rupees(credited)} actually "
                      f"reached the bank.", at)


def contract(instruments: int, gst_bps: int, floor: int, pct_bps: int,
             at=None) -> Line:
    return line(SAY, f"Reading this merchant's contract: "
                     f"{instruments} instruments, GST {gst_bps / 100:.0f}% of "
                     f"the fee, and I ignore gaps under "
                     f"{rupees(floor)} or {pct_bps / 100:.2f}%.", at)


def comparing(n: int, at=None) -> Line:
    return line(DO, f"Checking every one of the {n} deductions against that "
                    f"contract myself. This part is arithmetic - I do not "
                    f"guess at it.", at)


def rules_settled(settled: int, total: int, breakdown: dict, at=None) -> list[Line]:
    out = []
    for code, n in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        out.append(line(SAY, f"{n} x {code}", at))
    out.append(line(OK, f"{settled} of {total} settled by the rate card alone. "
                        f"Those never reach a language model.", at))
    return out


def nothing_to_judge(at=None) -> Line:
    return line(OK, "Nothing here needs judgement. That is the right answer "
                    "when a gateway has charged correctly.", at)


def needs_judgment(n: int, at=None) -> Line:
    return line(DO, f"{n} record{'s' if n != 1 else ''} the arithmetic cannot "
                    f"settle. Those need judgement, so I look at them one at "
                    f"a time.", at)


def looking_at(payment_id: str, instrument: str, amount: int, at=None) -> Line:
    return line(THINK, f"{payment_id} - {instrument}, {rupees(amount)}.", at)


def the_gap(actual_fee: int, expected_fee: int, delta: int,
            actual_tax: int = 0, expected_tax: int = 0, at=None) -> Line:
    """
    Say which leg actually moved.

    `delta` is fee plus GST. Describing a GST error in terms of the fee - "you
    were charged Rs 17.98 where the contract allows Rs 17.98, which is Rs 158.58
    too much" - is gibberish, and it is gibberish in the one place the product
    is supposed to be clearer than a settlement report.
    """
    fee_gap = actual_fee - expected_fee
    tax_gap = actual_tax - expected_tax

    if fee_gap == 0 and tax_gap == 0:
        return line(THINK, f"The fee is {rupees(actual_fee)} and "
                           f"{rupees(actual_fee)} is exactly right for this "
                           f"instrument. Every number matches - which is why "
                           f"arithmetic alone would call this clean.", at)

    if fee_gap == 0 and tax_gap:
        return line(THINK, f"The fee of {rupees(actual_fee)} is correct. The "
                           f"GST is not: {rupees(actual_tax)} charged where "
                           f"{rupees(expected_tax)} is due, which is "
                           f"{rupees(tax_gap)} too much.", at)

    if tax_gap and fee_gap:
        return line(THINK, f"Charged {rupees(actual_fee)} where the contract "
                           f"allows {rupees(expected_fee)}, and the GST follows "
                           f"the inflated fee. {rupees(delta)} too much in "
                           f"total.", at)

    return line(THINK, f"Charged {rupees(actual_fee)} where the contract "
                       f"allows {rupees(expected_fee)}. That is "
                       f"{rupees(fee_gap)} more than it should be.", at)


# What each action means to the person reading, rather than the enum value
# the code passes around. "my recommendation is to fix_books" is a database
# column leaking onto the screen.
_ACTION_WORDS = {
    "dispute": "ask the gateway for this back",
    "fix_books": "correct your books",
    "dismiss": "leave this alone",
    "escalate": "have a person look at it",
}


def evidence(rule: str, candidate: str, source: str, at=None) -> Line:
    from merchant.views import code_label

    # code_label, not .lower(): lowercasing the whole label turns
    # "Zero MDR violation" into "zero mdr violation" and undoes the point.
    label = code_label(candidate)
    label = label[0].lower() + label[1:] if label[:2].isupper() is False else label
    article = "an" if label[:1].lower() in "aeiou" else "a"
    return line(SAY, f"{rule.capitalize()} says this looks like {article} "
                     f"{label}, under {source}.", at)


def weighing(at=None) -> Line:
    return line(DO, "Two explanations could fit this gap. Choosing between "
                    "them is my job, not the calculator's.", at)


def tool_call(name: str, at=None) -> Line:
    return line(TOOL, f"Checking {name}() myself before I say it.", at)


def verdict(code: str, action: str, confidence: Optional[float], stake: int,
            tokens: int, seconds: float, at=None) -> Line:
    from merchant.views import code_label

    sure = f"{confidence * 100:.0f}% sure" if confidence is not None else "unsure"
    advice = _ACTION_WORDS.get(action, action.replace("_", " "))
    return line(OK, f"{code_label(code)} - {rupees(stake)} at stake. "
                    f"I would {advice}. ({sure}, {seconds:.0f}s)", at)


def reviewed_clean(at=None) -> Line:
    return line(SAY, "Checked: every figure above came from the calculator, "
                     "not from me.", at)


def reviewed_invented(figures: list[str], at=None) -> Line:
    return line(NOTE, f"I stated figures that were not in my evidence "
                      f"({', '.join(figures)}). Those are not trustworthy and "
                      f"my confidence has been capped.", at)


def reviewed_corrected(correction: str, at=None) -> Line:
    return line(NOTE, f"Corrected after review: {correction}", at)


def classify_failed(error: str, at=None) -> Line:
    return line(FAIL, f"I could not judge this one: {error[:110]}. "
                      f"Sending it to a person rather than assuming it is "
                      f"fine.", at)


def gate(auto: int, queued: int, at=None) -> Line:
    return line(DO, f"Guardrails: {auto} I can close, {queued} "
                    f"{'needs' if queued == 1 else 'need'} a person.", at)


def held(payment_id: str, reason: str, at=None) -> Line:
    return line(NOTE, f"{payment_id} held - {reason}", at)


def drafted(n: int, at=None) -> Line:
    return line(OK, f"{n} dispute letter{'s' if n != 1 else ''} drafted. I "
                    f"wrote the wording; the numbers came from the record.", at)


def finished(at=None) -> Line:
    return line(SAY, "Done. Nothing was changed - every line above is a "
                     "proposal for you to accept or reject.", at)


# --- replaying a finished run --------------------------------------------

# --- pacing a replay ------------------------------------------------------
#
# Stored timestamps are per RECORD, not per line, so replaying straight off them
# would jump in lumps. Instead the offsets are derived from what was actually
# measured: the fast steps get a fixed beat, and each record's think-time is the
# `latency_ms` the model really took. The pauses in a replay are real pauses.

BEAT_MS = 260              # between the quick, deterministic steps
EVIDENCE_MS = 340          # reading a piece of evidence


def build(store, run_id: str, rate_card: dict,
          paced: bool = False) -> list[Line]:
    """
    Reconstruct the narration of a completed audit.

    Same builders the runner used live, fed from storage instead of from the
    moment. Timestamps come from the audit log where a record has one.

    With `paced`, each line also carries the millisecond offset at which it
    would have appeared, so a replay runs at the pace the audit actually did.
    """
    conn = store.conn
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return [line(FAIL, f"No settlement {run_id}.")]

    findings = store.findings(run_id)
    if not findings:
        return [line(SAY, "This settlement has not been audited yet.")]

    started = run["created_at"]
    trail = {r["payment_id"]: r for r in store.audit_trail(run_id)}
    payments = {r["payment_id"]: r for r in conn.execute(
        "SELECT * FROM payments WHERE run_id = ?", (run_id,))}

    money = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) gross FROM payments"
        " WHERE run_id = ?", (run_id,)).fetchone()
    credited = conn.execute(
        "SELECT COALESCE(SUM(amount),0) a FROM bank_credits WHERE run_id = ?",
        (run_id,)).fetchone()["a"]

    clock_ms = 0

    def beat(entries, gap: int = BEAT_MS):
        """Place lines on the playback clock as they are appended."""
        nonlocal clock_ms
        out = entries if isinstance(entries, list) else [entries]
        for one in out:
            one.offset = clock_ms
            clock_ms += gap
        return out

    lines: list[Line] = []
    lines += beat([
        opening(run_id, started),
        loaded(money["n"], money["gross"], credited, started),
        contract(len(rate_card["instruments"]), rate_card["gst_rate_bps"],
                 rate_card["tolerance"]["floor_paise"],
                 rate_card["tolerance"]["pct_bps"], started),
        comparing(len(findings), started),
    ])

    by_rules = [f for f in findings if f["decided_by"] == "calculator"]
    by_agent = [f for f in findings if f["decided_by"] == "agent"]

    breakdown: dict[str, int] = {}
    for f in by_rules:
        breakdown[f["exception_code"]] = breakdown.get(f["exception_code"], 0) + 1
    lines += beat(rules_settled(len(by_rules), len(findings), breakdown, started))

    if not by_agent:
        lines += beat(nothing_to_judge(started))
    else:
        lines += beat(needs_judgment(len(by_agent), started))

    for f in by_agent:
        entry = trail.get(f["payment_id"])
        at = entry["decided_at"] if entry else started
        pay = payments.get(f["payment_id"])
        instrument = _instrument_label(pay, rate_card) if pay else ""

        lines += beat(looking_at(f["payment_id"], instrument,
                                 pay["amount"] if pay else 0, at))
        lines += beat(the_gap(f["actual_fee"], f["expected_fee"], f["delta"],
                              f["actual_tax"], f["expected_tax"], at))

        signals = json.loads(entry["signals"]) if entry and entry["signals"] else []
        for sig in signals:
            lines += beat(evidence(sig["rule"], sig["candidate_code"],
                                   sig["source"], at), EVIDENCE_MS)

        if entry is None:
            lines += beat(line(NOTE, "No audit trail for this record.", at))
            continue

        # The think-time is the real one. Everything the model did during it -
        # the weighing, the tool calls - is spread across the latency that was
        # actually measured, so a replay pauses exactly where the audit paused.
        think_ms = max(int(entry["latency_ms"] or 0), 600)
        tools = json.loads(entry["tool_calls"] or "[]")
        steps = 1 + len(tools)
        step_ms = think_ms // (steps + 1)

        lines += beat(weighing(at), step_ms)
        for tool in tools:
            lines += beat(tool_call(tool, at), step_ms)

        if entry["error"]:
            lines += beat(classify_failed(entry["error"], at))
            continue

        lines += beat(verdict(entry["exception_code"], entry["action"],
                              entry["confidence"], f["money_at_stake"],
                              entry["output_tokens"],
                              entry["latency_ms"] / 1000, at))

        invented = json.loads(entry["invented_figures"] or "[]")
        corrections = json.loads(entry["corrections"] or "[]")
        if invented:
            lines += beat(reviewed_invented(invented, at))
        elif corrections:
            for correction in corrections:
                lines += beat(reviewed_corrected(correction, at))
        else:
            lines += beat(reviewed_clean(at))

    queued = [f for f in findings if f["queued_for_human"]]
    lines += beat(gate(len(findings) - len(queued), len(queued), started))
    for f in queued:
        for reason in json.loads(f["queue_reasons"] or "[]"):
            lines += beat(held(f["payment_id"], reason, started))

    drafted_n = len([f for f in findings if f["dispute_text"]])
    if drafted_n:
        lines += beat(drafted(drafted_n, started))
    lines += beat(finished(started))

    if not paced:
        for one in lines:
            one.offset = 0
    return lines


def _instrument_label(pay, rate_card: dict) -> str:
    from engine.expected_value import Payment, classify_instrument

    key, _ = classify_instrument(Payment(
        payment_id=pay["payment_id"], amount=pay["amount"],
        method=pay["method"], card_network=pay["card_network"],
        card_type=pay["card_type"],
        is_international=bool(pay["is_international"]),
        upi_reference=pay["upi_reference"]))
    return rate_card["instruments"][key]["label"]
