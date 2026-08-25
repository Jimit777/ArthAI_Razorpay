"""
Measuring the agent against a known answer key.

## Why this is not on a merchant's settlement page

An accuracy percentage needs something to be accurate *against*. Real
settlements have no answer key - nobody knows which of a merchant's deductions
were wrong, which is the entire reason this product exists. A percentage shown
next to real findings would be invented.

The generator plants known errors and hands back the answer key, so a batch it
produced is the only place a match rate can honestly be computed. That makes
this a fact about the agent rather than about anyone's money, which is why it
lives under /admin.

## Two ways to run it, and why the free one is the default

    replay      re-score verdicts recorded earlier. No API calls, no cost.
    live        ask the agent for real. Costs money and needs a network.

A demo gets rehearsed a dozen times and the answers do not change between
rehearsals. Paying for every practice run is money spent re-deriving a result
already on disk.

## Why there is no rate-card-only mode

It was written and then removed. Run the calculator without the agent and the
thirteen records needing judgment come back UNEXPLAINED - but score() has no
verdicts to attribute them to, so it counts all sixty as calculator-settled and
reports 48/60. That reads as "80% accurate" when what actually happened is that
the rate card settled 47 records and half the system was never run.

The number is not wrong so much as it is measuring something nobody asked
about, and a page whose whole purpose is an honest match rate cannot carry one.
A replay is free anyway, and it already shows the calculator/agent split.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_N = 60
DEFAULT_SEED = 20260905                 # the seed demo_run.json was recorded on
DEFAULT_RECORDING = "demo_run.json"

SETTLEMENT = "settlement_audit"
GST = "gst_itc"
DEFAULT_AGENT = SETTLEMENT

# Where a live run parks its verdicts so every later rehearsal is free. The
# settlement agent's recording predates this and keeps its own path.
RECORDINGS = Path("recordings")

BENCHMARK_AGENTS: dict[str, dict] = {
    # judgment_records and approx_rupees are what a LIVE run actually costs.
    # The calculator settles most of a batch for nothing; only the records it
    # hands over are billed, and the two agents hand over different numbers of
    # them. Quoting one agent's cost against the other would be a small lie on
    # the one page whose whole job is not telling small lies.
    SETTLEMENT: {
        "name": "Settlement Deduction Auditor",
        "measures": "Gateway fees and GST on them, against the rate card.",
        "recording": DEFAULT_RECORDING,
        "judgment_records": 13,
        "approx_rupees": 45,
    },
    GST: {
        "name": "GST Input Credit Reconciler",
        "measures": "Purchase register against GSTR-2B, invoice by invoice.",
        "recording": str(RECORDINGS / "gst_itc.json"),
        "judgment_records": 6,
        "approx_rupees": 22,
    },
}


def recording_path(agent_id: str) -> str:
    spec = BENCHMARK_AGENTS.get(agent_id) or BENCHMARK_AGENTS[DEFAULT_AGENT]
    return spec["recording"]

MODE_REPLAY = "replay"
MODE_LIVE = "live"

MODE_LABEL = {
    MODE_REPLAY: "Replayed a recording",
    MODE_LIVE: "Live agent",
}

FREE_MODES = {MODE_REPLAY}

BENCHMARK_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmarks (
  benchmark_id TEXT PRIMARY KEY,
  at           INTEGER,
  agent_id     TEXT,
  mode         TEXT,              -- replay | rules | live
  n_records    INTEGER,
  seed         INTEGER,
  model        TEXT,
  effort       TEXT,
  duration_ms  INTEGER,
  -- the headline numbers, as columns so they can be queried and charted
  total        INTEGER,
  correct      INTEGER,
  by_calculator INTEGER,
  by_calculator_correct INTEGER,
  by_agent     INTEGER,
  by_agent_correct INTEGER,
  anomalies    INTEGER,
  anomalies_caught INTEGER,
  anomalies_missed INTEGER,
  decoys       INTEGER,
  decoys_dismissed INTEGER,
  clean        INTEGER,
  clean_correct INTEGER,
  false_accusations INTEGER,
  failed_calls INTEGER,
  queued_for_human INTEGER,
  auto_resolved INTEGER,
  recoverable_paise INTEGER,
  -- the lists, which are the honest part
  detail       TEXT,              -- JSON: by_code, false_accusations, misses
  ran_by       TEXT
);
"""


@dataclass
class Progress:
    state: str = "running"          # running | done | failed
    phase: str = ""
    done: int = 0
    total: int = 0
    note: str = ""
    benchmark_id: Optional[str] = None
    error: str = ""


class Benchmarks:
    """Stored benchmark results. Nothing here is ever a merchant's data."""

    def __init__(self, conn):
        from merchant.businesses import _add_column

        self.conn = conn
        self.conn.executescript(BENCHMARK_SCHEMA)
        # The table predates the second agent, and CREATE TABLE IF NOT EXISTS
        # silently does nothing to a table that already exists - so a new
        # column never reaches a database made before it. Same fix as
        # businesses.archived_at, and the same trap caught twice now.
        _add_column(conn, "benchmarks", "agent_id", "TEXT")
        self.conn.commit()

    def record(self, card, *, mode: str, n: int, seed: int, model: str,
               effort: str, duration_ms: int, ran_by: str = "",
               agent_id: str = DEFAULT_AGENT) -> str:
        import secrets

        benchmark_id = f"bench_{secrets.token_hex(5)}"
        detail = json.dumps({
            "by_code": card.by_code,
            "false_accusations": card.false_accusations,
            "misses": card.misses,
            "miscategorised": card.miscategorised,
        })
        self.conn.execute(
            "INSERT INTO benchmarks (benchmark_id, at, agent_id, mode, n_records, seed,"
            " model, effort, duration_ms, total, correct, by_calculator,"
            " by_calculator_correct, by_agent, by_agent_correct, anomalies,"
            " anomalies_caught, anomalies_missed, decoys, decoys_dismissed,"
            " clean, clean_correct, false_accusations, failed_calls,"
            " queued_for_human, auto_resolved, recoverable_paise, detail,"
            " ran_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (benchmark_id, int(time.time()), agent_id, mode, n, seed, model, effort,
             duration_ms, card.total, card.correct, card.by_calculator,
             card.by_calculator_correct, card.by_agent, card.by_agent_correct,
             card.anomalies, card.anomalies_caught, card.anomalies_missed,
             card.decoys, card.decoys_dismissed, card.clean, card.clean_correct,
             len(card.false_accusations), card.failed_calls,
             card.queued_for_human, card.auto_resolved, card.recoverable_paise,
             detail, ran_by))
        self.conn.commit()
        return benchmark_id

    def latest(self, agent_id: Optional[str] = None):
        if agent_id is None:
            return self.conn.execute(
                "SELECT * FROM benchmarks ORDER BY at DESC LIMIT 1").fetchone()
        return self.conn.execute(
            "SELECT * FROM benchmarks WHERE agent_id = ?"
            " ORDER BY at DESC LIMIT 1", (agent_id,)).fetchone()

    def get(self, benchmark_id: str):
        return self.conn.execute(
            "SELECT * FROM benchmarks WHERE benchmark_id = ?",
            (benchmark_id,)).fetchone()

    def history(self, limit: int = 20, agent_id: Optional[str] = None) -> list:
        if agent_id is None:
            return self.conn.execute(
                "SELECT * FROM benchmarks ORDER BY at DESC LIMIT ?",
                (limit,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM benchmarks WHERE agent_id = ?"
            " ORDER BY at DESC LIMIT ?", (agent_id, limit)).fetchall()


def recording_available(path: str = DEFAULT_RECORDING) -> bool:
    return Path(path).exists()


def has_recording(agent_id: str) -> bool:
    return Path(recording_path(agent_id)).exists()


def run_benchmark(*, agent_id: str = DEFAULT_AGENT, mode: str = MODE_REPLAY,
                  n: int = DEFAULT_N, seed: int = DEFAULT_SEED,
                  effort: str = "medium", model: str = "opus",
                  recording: Optional[str] = None,
                  on_progress: Optional[Callable[..., None]] = None):
    """Dispatch to whichever agent's benchmark was asked for."""
    if agent_id == GST:
        return _run_gst(mode=mode, n=n, seed=seed, effort=effort, model=model,
                        recording=recording or recording_path(GST),
                        on_progress=on_progress)
    return _run_settlement(mode=mode, n=n, seed=seed, effort=effort,
                           model=model,
                           recording=recording or DEFAULT_RECORDING,
                           on_progress=on_progress)


def _save_recording(path: str, verdicts) -> None:
    """
    Park a live run's verdicts so every rehearsal after it is free.

    The settlement agent's demo_run.json was made by hand with audit.py --save.
    Doing it automatically means the second agent never needs that step, and a
    demo is never one API outage away from having nothing to show.
    """
    from dataclasses import asdict

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        json.dump([asdict(v) for v in verdicts], f, indent=1)


def _run_gst(*, mode: str, n: int, seed: int, effort: str, model: str,
             recording: str, on_progress: Optional[Callable[..., None]]):
    """
    Purchase register against GSTR-2B.

    Same three phases as the settlement run - generate, check, score - but the
    checking step joins two datasets rather than recomputing one number, so the
    calculator settles a different shape of record and hands over a different
    shape of question.
    """
    from agent.gst_classifier import ITCVerdict
    from engine.gst.detector import detect_batch
    from engine.gst.gate import gate_batch
    from engine.gst.generator import generate_batch
    from engine.gst.scoring import score

    def report(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.time()

    report(phase="Generating a purchase register with planted errors",
           done=0, total=n)
    batch, ground_truth = generate_batch(n, seed)

    report(phase="Matching every invoice against GSTR-2B", total=n)
    variances = detect_batch(batch)
    open_ones = [v for v in variances if v.needs_agent]

    verdicts: list = []

    if mode == MODE_REPLAY:
        if not Path(recording).exists():
            raise ValueError(
                f"no recording at {recording}. Run the agent live once and it "
                f"will record itself, after which replaying is free.")
        with open(recording) as f:
            verdicts = [ITCVerdict(**d) for d in json.load(f)]
        saved = {v.invoice_id for v in verdicts}
        wanted = {v.invoice_id for v in open_ones}
        if saved != wanted:
            raise ValueError(
                f"{recording} holds {len(saved)} verdicts but this batch needs "
                f"{len(wanted)}. It was recorded on a different batch - "
                f"re-record it, or check n and seed.")
        report(phase=f"Replaying {len(verdicts)} recorded decisions",
               done=n, total=n, note="no API calls, no cost")

    elif mode == MODE_LIVE:
        from agent.gst_classifier import MODELS, ClaudeITCClassifier

        classifier = ClaudeITCClassifier(batch, effort=effort,
                                         model=MODELS[model])
        done = 0
        for variance in open_ones:
            done += 1
            report(phase=f"Asking the agent about {variance.invoice_id}",
                   done=done, total=len(open_ones))
            verdicts.append(classifier.classify(variance))
        if verdicts and not any(v.error for v in verdicts):
            _save_recording(recording, verdicts)
    else:
        raise ValueError(f"unknown mode {mode}")

    report(phase="Applying the guardrail gate", done=n, total=n)
    decisions = gate_batch(variances, verdicts)

    report(phase="Scoring against the answer key", done=n, total=n)
    card = score(decisions, ground_truth, variances)

    return card, int((time.time() - started) * 1000)


def _run_settlement(*, mode: str, n: int, seed: int, effort: str, model: str,
                    recording: str,
                    on_progress: Optional[Callable[..., None]]):
    """
    Generate a batch with planted errors, audit it, and score the result
    against the answer key the generator handed back.

    Returns (scorecard, duration_ms). Raises ValueError if the mode cannot run.
    """
    from agent.classifier import Verdict
    from engine.detector import detect_batch
    from engine.gate import gate_batch
    from engine.scoring import score
    from generator.synthetic import generate_batch

    def report(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.time()

    report(phase="Generating a batch with planted errors", done=0, total=n)
    batch, ground_truth = generate_batch(n, seed)

    report(phase="Checking every deduction against the rate card", total=n)
    variances = detect_batch(batch)
    open_ones = [v for v in variances if v.needs_agent]

    verdicts: list = []

    if mode == MODE_REPLAY:
        if not Path(recording).exists():
            raise ValueError(f"no recording at {recording}")
        with open(recording) as f:
            verdicts = [Verdict(**d) for d in json.load(f)]
        saved = {v.payment_id for v in verdicts}
        wanted = {v.payment_id for v in open_ones}
        if saved != wanted:
            # Replaying a recording of a different batch would score one set of
            # answers against another set of questions and report a number.
            raise ValueError(
                f"{recording} holds {len(saved)} verdicts but this batch needs "
                f"{len(wanted)}. It was recorded on a different batch - "
                f"re-record it, or check n and seed.")
        report(phase=f"Replaying {len(verdicts)} recorded decisions",
               done=n, total=n, note="no API calls, no cost")

    elif mode == MODE_LIVE:
        from agent.classifier import MODELS, ClaudeClassifier, classify_batch

        classifier = ClaudeClassifier(batch, effort=effort,
                                      model=MODELS[model])
        done = 0
        for variance in open_ones:
            done += 1
            report(phase=f"Asking the agent about {variance.payment_id}",
                   done=done, total=len(open_ones))
            verdicts.append(classifier.classify(variance))
    else:
        raise ValueError(f"unknown mode {mode}")

    report(phase="Applying the guardrail gate", done=n, total=n)
    decisions = gate_batch(variances, verdicts, batch.rate_card)

    report(phase="Scoring against the answer key", done=n, total=n)
    card = score(decisions, ground_truth, variances)

    return card, int((time.time() - started) * 1000)
