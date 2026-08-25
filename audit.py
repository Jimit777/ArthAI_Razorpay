#!/usr/bin/env python3
"""
Run the auditor end to end.

  python audit.py --preview     show exactly what would be sent to Claude,
                                without calling it (no API key needed)
  python audit.py               generate, detect, and classify a batch

The report and the scoring against ground truth are checkpoints 7 and 9. This
is the pipeline as far as it goes today.
"""

from __future__ import annotations

import argparse
import os
import sys

from agent.classifier import DEFAULT_EFFORT
from agent.prompt import render_variance, system_prompt
from engine.detector import detect_batch, print_audit
from engine.expected_value import rupees
from generator.synthetic import generate_batch


def preview(variances, limit: int) -> None:
    print("=" * 78)
    print("SYSTEM PROMPT (cached prefix, identical on every record)")
    print("=" * 78)
    print(system_prompt())
    for v in variances[:limit]:
        print("=" * 78)
        print(f"USER MESSAGE for {v.payment_id}")
        print("=" * 78)
        print(render_variance(v))
        print()


def run(batch, all_variances, open_variances, ground_truth, effort: str,
        save: str | None = None, replay: str | None = None,
        model: str = "opus", use_batch: bool = False,
        db: str | None = None, dispute_limit: int = 2) -> None:
    import json
    from dataclasses import asdict

    from agent.classifier import Verdict, classify_batch
    from engine.gate import gate_batch
    from engine.scoring import print_scorecard, score

    if replay:
        # Replay a saved run instead of paying for it again.
        #
        # A demo gets rehearsed a dozen times and the answers do not change
        # between rehearsals. Paying the API on every practice run is money
        # spent re-deriving a result already on disk - and it makes the live
        # demo dependent on the venue's wifi, which is its own argument.
        # Record once, rehearse free, and keep the live run for validation.
        with open(replay) as f:
            verdicts = [Verdict(**d) for d in json.load(f)]
        saved = {v.payment_id for v in verdicts}
        wanted = {v.payment_id for v in open_variances}
        if saved != wanted:
            print(f"\n  !! {replay} does not match this batch.")
            print(f"     It holds {len(saved)} verdicts; this batch needs {len(wanted)}.")
            print( "     Re-record it with --save, or check --n and --seed match.\n")
            return
        print(f"\n  replaying {len(verdicts)} saved verdicts from {replay}"
              f"  (no API calls, no cost)\n")
    else:
        from agent.classifier import MODELS, ClaudeClassifier

        model_id = MODELS[model]
        print(f"\nclassifying {len(open_variances)} records with the agent")
        print(f"  model {model_id}, effort {effort}"
              + (", via the Batches API (half price, slower)" if use_batch else ""))
        if model != "opus":
            print("  NOTE: not the demo model. Re-measure on opus before "
                  "quoting this number.")
        print()

        if use_batch:
            from agent.batch_classifier import BatchClassifier

            verdicts = BatchClassifier(batch, model=model_id,
                                       effort=effort).classify_all(open_variances)
        else:
            classifier = ClaudeClassifier(batch, effort=effort, model=model_id)
            verdicts = classify_batch(open_variances, classifier, progress=True)

    if save:
        with open(save, "w") as f:
            json.dump([asdict(v) for v in verdicts], f, indent=1)
        print(f"\n  verdicts saved to {save}")

    print()
    for verdict in verdicts:
        flag = "  <- REVIEW" if verdict.corrections else ""
        print(f"  {verdict.payment_id}  {verdict.exception_code:<24}"
              f"{verdict.action:<11} confidence {verdict.confidence:.2f}{flag}")
        print(f"      {verdict.reasoning}")
        print(f"      rule: {verdict.rule_cited}")
        if verdict.tool_calls:
            print(f"      tools used: {', '.join(verdict.tool_calls)}")
        for correction in verdict.corrections:
            print(f"      CORRECTED: {correction}")
        print()

    cached = sum(v.cache_read_tokens for v in verdicts)
    used = sum(v.input_tokens for v in verdicts)
    out = sum(v.output_tokens for v in verdicts)
    if not replay:
        from agent.pricing import Usage

        spent = Usage(input_tokens=used, output_tokens=out,
                      cache_read_tokens=cached, calls=len(verdicts),
                      batched=use_batch)
        print(f"  {used:,} new input tokens, {cached:,} read from cache, "
              f"{out:,} output  ->  ${spent.usd:.3f} "
              f"(about Rs {spent.rupees:.0f})")
    failed = [v for v in verdicts if v.error]
    if failed:
        print(f"  {len(failed)} records failed and were escalated")

    # --- the guardrail gate, then the score --------------------------------
    from agent.dispute import attach_disputes

    decisions = gate_batch(all_variances, verdicts, batch.rate_card)
    queued = [d for d in decisions if d.queued_for_human]
    disputes = attach_disputes(all_variances, verdicts, decisions)

    print()
    print("=" * 70)
    print("GUARDRAIL GATE")
    print("=" * 70)
    print(f"\n  {len(decisions) - len(queued)} auto-resolved, "
          f"{len(queued)} queued for a human\n")
    for d in queued:
        print(f"  {d.payment_id}  {d.exception_code:<24}{rupees(d.money_at_stake):>12}")
        for reason in d.reasons:
            print(f"      {reason}")

    if db:
        from agent.classifier import MODELS
        from engine.store import Store

        with Store(db) as store:
            run_id = store.save_run(batch, model=MODELS.get(model, model),
                                    effort=effort, via_batch=use_batch)
            store.save_findings(run_id, decisions, all_variances, verdicts,
                                disputes)
            queued = len(store.findings(run_id, queued_only=True))
        print()
        print(f"  saved to {db} as {run_id}")
        print(f"  {len(decisions)} findings, {queued} in the human queue, "
              f"{len(verdicts)} agent decisions in the audit log")

    if disputes:
        print()
        print("=" * 70)
        print(f"DISPUTE MESSAGES  ({len(disputes)} ready to send)")
        print("=" * 70)
        for pid, message in list(disputes.items())[:dispute_limit]:
            print()
            print(message)
            print()
        if len(disputes) > dispute_limit:
            print(f"  ... and {len(disputes) - dispute_limit} more"
                  f" (use --db to keep them all)")

    print_scorecard(score(decisions, ground_truth, all_variances))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--effort", default=DEFAULT_EFFORT,
                    choices=["low", "medium", "high", "xhigh", "max"],
                    help="how hard the agent thinks. Thinking bills as output "
                         "and output is most of the cost, so this is the main "
                         "cost dial. Default medium, validated at "
                         "100%% across five batches.")   # argparse %-formats help
    ap.add_argument("--model", default="opus", choices=["opus", "sonnet"],
                    help="opus (default) is the model to quote a number on. "
                         "sonnet is cheaper for iterating on prompts and rules "
                         "- re-measure on opus before believing the result.")
    ap.add_argument("--preview", action="store_true",
                    help="print the prompts instead of calling the API")
    ap.add_argument("--limit", type=int, default=3,
                    help="how many records to show in --preview")
    ap.add_argument("--save", metavar="PATH",
                    help="write the agent's verdicts to a JSON file")
    ap.add_argument("--replay", metavar="PATH",
                    help="score a previously saved run without calling the API")
    ap.add_argument("--db", metavar="PATH", nargs="?", const="auditor.db",
                    help="persist the run to a SQLite file (default auditor.db). "
                         "Holds the findings, the human queue and the full "
                         "audit trail.")
    ap.add_argument("--disputes", type=int, default=2, metavar="N",
                    help="how many dispute messages to print in full. "
                         "All of them are stored when --db is used.")
    ap.add_argument("--batch", action="store_true",
                    help="run through the Message Batches API at half price. "
                         "Takes minutes rather than seconds - use it for "
                         "validation runs, never for a live demo.")
    args = ap.parse_args()

    batch, ground_truth = generate_batch(args.n, args.seed)
    variances = detect_batch(batch)
    open_ones = [v for v in variances if v.needs_agent]

    print_audit(variances)

    if args.preview:
        preview(open_ones, args.limit)
        return 0

    if not args.replay and not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nNo ANTHROPIC_API_KEY is set, so the agent cannot run.\n")
        print("  export ANTHROPIC_API_KEY=sk-ant-...   then re-run")
        print("  or:  python audit.py --preview        to see the prompts\n")
        return 1

    run(batch, variances, open_ones, ground_truth, args.effort, args.save,
        args.replay, args.model, args.batch, args.db, args.disputes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
