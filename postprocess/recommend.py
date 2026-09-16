#!/usr/bin/env python
"""clawhound recommend: turn a promptfoo results file into a decision.

promptfoo runs the evals across every provider and shows you the numbers. It
does not DECIDE. This reads `promptfoo eval --output results.json` and answers
the question a buyer actually has: which model should I run, does it clear my
bar, and what does it cost. It prints a ranked table and a recommendation, and
writes a self-contained HTML report whose centerpiece is a pair of side-by-side
frontiers, cost-vs-quality and latency-vs-quality, sharing one model color
legend and one zoomed pass-rate axis (inline SVG, no external libraries, opens
in any browser). The per-service routing table shows BOTH the cheapest and the
fastest model that clears the bar for each category.

Alongside the per-model view it prints a per-TEST "Suite health" view (also in
the HTML report), which turns the matrix on its side to grade the TESTS: a
DISCRIMINATING test every model passed no longer ranks anything (flagged
saturated, harden or retire), and a FLOOR test any model failed is the
regression alarm firing (flagged as a regression or coverage gap). A floor test
that all models pass is the alarm working and is never flagged.

Two layers of tests are expected (see docs/eval-design-guide.md):
  floor          deterministic must-pass tests. Scored by promptfoo `success`
                 (boolean). The bar is a pass-rate you must clear (default 1.0).
  discriminating graded tests (g-eval / llm-rubric). Scored by promptfoo `score`
                 (0 to 1). Used to rank and, optionally, as a second gate.

A test is assigned to a layer by, in order: testCase.metadata.layer, then
metadata.layer, then vars.layer / vars.__layer. If nothing tags the layers, the
tool falls back to treating every test as floor for the pass-rate and every
score as discriminating, and says so.

Schema note: written to the promptfoo EvaluateSummaryV3 output (top-level
`results[]`, each with provider.id, success, score, cost, tokenUsage). Parsing
is defensive; validation against a live promptfoo run is still pending.

Usage:
  python recommend.py results.json [--bar 1.0] [--disc-bar 0.0]
                       [--optimize cost|latency]
                       [--incumbent provider:model] [--out report.html]
  python recommend.py --selftest   # run the built-in sample-results test

--optimize now only sets which lens the owner HEADLINE leads with: cost (default,
cheapest that clears the bar) or latency (fastest by median per-call latency).
The HTML report ALWAYS renders both frontier charts and both per-service routing
picks (cheapest and fastest) regardless of this choice. The bar is unchanged.

Implementation note: this file is a thin CLI entrypoint. The actual logic lives
in the recommend_lib/ package next to this script (parsing, aggregate, routing,
formatting, charts, report, cli, selftest) - split out for maintainability.
Kept as a single top-level script (not `python -m recommend_lib`) so the exact
invocation documented in AGENTS.md / README / templates keeps working unchanged.
"""

import argparse
import json
import os
import sys

from recommend_lib.parsing import load_records, rec_model
from recommend_lib.aggregate import aggregate, aggregate_tests, aggregate_by_category
from recommend_lib.routing import load_category_labels, recommend, benchmark_deltas
from recommend_lib.cli import print_report, print_health, print_routing, print_benchmark
from recommend_lib.report import render_html
from recommend_lib.selftest import _selftest


def main():
    ap = argparse.ArgumentParser(prog="recommend")
    ap.add_argument("results", nargs="?",
                    help="promptfoo results JSON (promptfoo eval --output)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the built-in sample-results test (no file needed) and exit")
    ap.add_argument("--bar", type=float, default=1.0,
                    help="floor pass-rate a model must clear (default 1.0)")
    ap.add_argument("--disc-bar", type=float, default=0.0,
                    help="optional minimum discriminating score (default 0, off)")
    ap.add_argument("--optimize", choices=("cost", "latency"), default="cost",
                    help="which lens the owner HEADLINE leads with: cost (default, "
                         "cheapest that clears the bar) or latency (fastest by "
                         "median latency that clears the bar). This ONLY sets the "
                         "headline emphasis. The HTML report always shows BOTH "
                         "frontier charts (cost-x and latency-x) and BOTH routing "
                         "picks (cheapest and fastest) per service, whatever you "
                         "choose. The bar itself is unchanged.")
    ap.add_argument("--incumbent", default=None,
                    help="provider:model you run today, marked 'you are here'")
    ap.add_argument("--compare", default=None,
                    help="comma-separated provider:model ids to focus the report on, "
                         "used together with --incumbent as the benchmark. Filters "
                         "everything (the model table, frontier charts, routing, "
                         "suite health) down to just the incumbent plus these, and "
                         "adds a benchmark delta table up top: each compare model's "
                         "disc score, cost, and latency stated as a percent vs the "
                         "incumbent, not just the raw numbers side by side. For the "
                         "'a new model shipped, should I upgrade from what I run "
                         "today' question, or 'how does my pick compare to similar-"
                         "tier alternatives across vendors' - a focused view instead "
                         "of the full field. An id not present in the results is "
                         "skipped with a warning, not a hard error.")
    ap.add_argument("--top-n", type=int, default=8,
                    help="show the top N models (by floor) as individual dots on the "
                         "frontier charts; collapse the rest into one gray '+X more' "
                         "point at the best remainder's coordinates (default 8; 0 "
                         "disables grouping)")
    ap.add_argument("--out", default="report.html", help="HTML report path")
    ap.add_argument("--latency-ceiling", type=float, default=90.0,
                    help="practical viability ceiling in seconds (default 90.0). A "
                         "model whose median latency EXCEEDS this is never the "
                         "recommended pick and is marked (daggered) with a warning "
                         "in every table, even when it would otherwise be cheapest or "
                         "clear the floor bar - correct-but-too-slow is a real, "
                         "distinct verdict from wrong or expensive. Its floor and disc "
                         "scores are still shown and still count; only recommendation "
                         "eligibility is gated. Pass 0 to disable the ceiling entirely "
                         "(see every model's raw numbers with no latency judgment, "
                         "e.g. to gauge how a model performs before judging whether "
                         "your current hardware can actually run it in practice).")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    if not args.results:
        ap.error("results file is required (or pass --selftest)")
    if not os.path.exists(args.results):
        sys.exit("results file not found: " + args.results)
    records = load_records(args.results)
    if not records:
        sys.exit("no evaluation records found in " + args.results)

    compare_ids = None
    if args.compare:
        if not args.incumbent:
            ap.error("--compare requires --incumbent (the benchmark model)")
        present = {rec_model(r) for r in records}
        if args.incumbent not in present:
            sys.exit("--incumbent " + args.incumbent + " not found in results")
        requested = [c.strip() for c in args.compare.split(",") if c.strip()]
        missing = [c for c in requested if c not in present]
        if missing:
            print("Warning: not present in results, skipped: " + ", ".join(missing),
                  file=sys.stderr)
        compare_ids = [c for c in requested if c in present]
        if not compare_ids:
            sys.exit("none of the --compare models are present in results")
        scope = {args.incumbent} | set(compare_ids)
        records = [r for r in records if rec_model(r) in scope]

    # The graded judge is pinned in the config; read it so the run-cost panel can
    # estimate grading spend (promptfoo does not price grading itself).
    judge_id = None
    try:
        _cfg = json.load(open(args.results, encoding="utf-8")).get("config", {})
        _jp = ((_cfg.get("defaultTest") or {}).get("options") or {}).get("provider")
        judge_id = _jp.get("id") if isinstance(_jp, dict) else _jp
    except Exception:
        judge_id = None

    agg, layered = aggregate(records)
    tests = aggregate_tests(records)
    cat_aggs, categorized = aggregate_by_category(records)
    labels = load_category_labels(args.results)
    rec = recommend(agg, args.bar, args.disc_bar, args.optimize, args.latency_ceiling)
    print_report(agg, layered, args.bar, args.disc_bar, rec, args.incumbent, args.optimize,
                latency_ceiling=args.latency_ceiling)
    print_routing(cat_aggs, categorized, args.bar, args.disc_bar, args.optimize,
                  latency_ceiling=args.latency_ceiling)
    print_health(tests, layered)

    deltas = None
    if compare_ids:
        deltas = benchmark_deltas(agg, args.incumbent, compare_ids)
        print_benchmark(args.incumbent, compare_ids, deltas)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(agg, layered, args.bar, args.disc_bar, rec,
                            args.incumbent, tests, cat_aggs, categorized, args.optimize,
                            records=records, labels=labels, judge_id=judge_id,
                            top_n=args.top_n, latency_ceiling=args.latency_ceiling,
                            compare_ids=compare_ids, deltas=deltas))
    print("\nHTML report: " + args.out)


if __name__ == "__main__":
    main()
