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
"""

import argparse
import html
import json
import os
import sys


# ----------------------------------------------------------------------------
# Load + parse (defensive: promptfoo has shipped a few output shapes)
# ----------------------------------------------------------------------------

def load_records(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # V3: {"results": [ ... ]}. Some exports nest {"results": {"results": [...]}}.
    # A raw list is also accepted.
    if isinstance(data, list):
        return data
    res = data.get("results", data)
    if isinstance(res, dict):
        res = res.get("results", [])
    return res if isinstance(res, list) else []


def rec_model(r):
    p = r.get("provider")
    if isinstance(p, dict):
        return p.get("id") or p.get("label") or "unknown"
    if isinstance(p, str):
        return p
    # older/table shapes sometimes carry the label elsewhere
    return r.get("providerId") or r.get("model") or "unknown"


def rec_layer(r):
    for holder in (r.get("testCase"), r, {"metadata": {"layer": None}}):
        if isinstance(holder, dict):
            meta = holder.get("metadata")
            if isinstance(meta, dict) and meta.get("layer"):
                return str(meta["layer"]).lower()
    v = r.get("vars")
    if isinstance(v, dict):
        lay = v.get("layer") or v.get("__layer")
        if lay:
            return str(lay).lower()
    return None


def rec_category(r):
    """Read a test's category the same defensive way rec_layer reads its layer.

    Order: testCase.metadata.category, then metadata.category, then
    vars.category. Returns None when nothing tags a category; callers label a
    None-category test "uncategorized". The `categorized` flag (any test with a
    non-None category) is what decides whether per-category routing runs at all.
    """
    for holder in (r.get("testCase"), r, {"metadata": {"category": None}}):
        if isinstance(holder, dict):
            meta = holder.get("metadata")
            if isinstance(meta, dict) and meta.get("category"):
                return str(meta["category"]).lower()
    v = r.get("vars")
    if isinstance(v, dict):
        cat = v.get("category")
        if cat:
            return str(cat).lower()
    return None


def rec_test_key(r):
    """Identify a test ACROSS models, defensively.

    A promptfoo result carries the same test under every provider, so to build a
    per-test view we need a stable key that is identical across providers. Try, in
    order: the test description, an explicit metadata id, promptfoo's testIdx, and
    finally a fingerprint of the test vars. Never raises on a missing field.
    """
    tc = r.get("testCase") if isinstance(r.get("testCase"), dict) else {}
    desc = tc.get("description") or r.get("description")
    if desc:
        return str(desc)
    for holder in (tc, r):
        if isinstance(holder, dict):
            meta = holder.get("metadata")
            if isinstance(meta, dict) and meta.get("id"):
                return str(meta["id"])
    idx = r.get("testIdx")
    if idx is not None:
        return "test#" + str(idx)
    v = tc.get("vars") or r.get("vars")
    if isinstance(v, dict) and v:
        try:
            return "vars:" + json.dumps(v, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return "vars:" + str(sorted(v.items()))
    return "unknown-test"


def rec_cost(r):
    c = r.get("cost")
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def rec_latency(r):
    """Per-call latency in milliseconds, defensively.

    promptfoo records it at the top-level `latencyMs`; older/other shapes carry
    it under `response.latencyMs` or `metrics.latencyMs`. Returns None when no
    latency is present or it does not parse as a number.
    """
    candidates = [r.get("latencyMs")]
    resp = r.get("response")
    if isinstance(resp, dict):
        candidates.append(resp.get("latencyMs"))
    met = r.get("metrics")
    if isinstance(met, dict):
        candidates.append(met.get("latencyMs"))
    for c in candidates:
        if c is None:
            continue
        try:
            return float(c)
        except (TypeError, ValueError):
            continue
    return None


def _median(vals):
    """Median of a list of numbers, or None if empty. Robust to outliers, which
    is why we prefer it over the mean for latency. Note: repeated runs
    (promptfoo --repeat) make both the pass-rate and the median latency more
    reliable; this tool just reads whatever samples are in the results file."""
    xs = sorted(v for v in vals if v is not None)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


# ----------------------------------------------------------------------------
# Aggregate per model
# ----------------------------------------------------------------------------

def aggregate(records):
    layered = any(rec_layer(r) is not None for r in records)
    models = {}
    for r in records:
        m = rec_model(r)
        a = models.setdefault(m, {
            "floor_pass": 0, "floor_n": 0,
            "disc_sum": 0.0, "disc_n": 0,
            "cost_sum": 0.0, "cost_n": 0, "n": 0,
            "latencies": [],
        })
        a["n"] += 1
        layer = rec_layer(r)
        success = bool(r.get("success"))
        score = r.get("score")
        if layered:
            if layer == "floor":
                a["floor_n"] += 1
                a["floor_pass"] += 1 if success else 0
            elif layer == "discriminating":
                if isinstance(score, (int, float)):
                    a["disc_sum"] += float(score)
                    a["disc_n"] += 1
        else:
            # No layer tags: pass-rate from success across all, score across all.
            a["floor_n"] += 1
            a["floor_pass"] += 1 if success else 0
            if isinstance(score, (int, float)):
                a["disc_sum"] += float(score)
                a["disc_n"] += 1
        c = rec_cost(r)
        if c is not None:
            a["cost_sum"] += c
            a["cost_n"] += 1
        lat = rec_latency(r)
        if lat is not None:
            a["latencies"].append(lat)

    out = {}
    for m, a in models.items():
        floor_rate = (a["floor_pass"] / a["floor_n"]) if a["floor_n"] else None
        disc = (a["disc_sum"] / a["disc_n"]) if a["disc_n"] else None
        # cost per test uses the count of records that reported a cost
        cpt = (a["cost_sum"] / a["cost_n"]) if a["cost_n"] else None
        # median latency is robust to slow-outlier calls; keep ms and seconds
        lat_ms = _median(a["latencies"])
        lat_s = None if lat_ms is None else lat_ms / 1000.0
        out[m] = {
            "floor_rate": floor_rate,
            "disc": disc,
            "cost_per_test": cpt,
            "cost_known": a["cost_n"] > 0,
            "latency_ms": lat_ms,
            "latency_s": lat_s,
            "latency_known": len(a["latencies"]) > 0,
            "n": a["n"],
        }
    return out, layered


# ----------------------------------------------------------------------------
# Aggregate per test (across models): the "suite health" view. The per-model
# aggregate answers "which model to run"; this answers "which of my TESTS are
# still doing their job." A discriminating test every model passes has stopped
# ranking anything; a floor test any model fails is the regression alarm firing.
# ----------------------------------------------------------------------------

def aggregate_tests(records):
    """Group records by test and count how many models passed each.

    Pass is layer-dependent: floor tests use the promptfoo `success` boolean;
    discriminating tests use `score` >= 0.5. Untagged tests fall back to
    `success` and are left out of both health flags (layer stays None), which
    preserves the untagged-layers fallback: no misleading saturation/regression
    calls when nothing tagged the layers.
    """
    tests = {}
    for r in records:
        key = rec_test_key(r)
        layer = rec_layer(r)
        t = tests.setdefault(key, {
            "layer": layer,
            "n_models": 0,
            "n_passed": 0,
            # Repeated runs (promptfoo --repeat) carry the same (model, test)
            # many times. Count runs and fails PER MODEL so the health view can
            # say "fails 5/5" once instead of printing one line per repeat.
            "runs_by_model": {},
            "fails_by_model": {},
        })
        if t["layer"] is None and layer is not None:
            t["layer"] = layer
        t["n_models"] += 1
        model = rec_model(r)
        t["runs_by_model"][model] = t["runs_by_model"].get(model, 0) + 1
        score = r.get("score")
        if layer == "discriminating":
            passed = isinstance(score, (int, float)) and float(score) >= 0.5
        else:
            # floor, or untagged: the deterministic must-pass boolean
            passed = bool(r.get("success"))
        if passed:
            t["n_passed"] += 1
        else:
            t["fails_by_model"][model] = t["fails_by_model"].get(model, 0) + 1
    return tests


def suite_health(tests):
    """Return (saturated, regressions) from the per-test aggregate.

    saturated   list of discriminating test keys that EVERY model passed. These
                no longer rank anything, so they should be hardened or retired.
    regressions list of (model, test key, fails, runs) where a FLOOR test failed
                for a model. DEDUPED across repeats: one tuple per (model, test)
                with the fail count and total run count, never one per repeat. A
                floor test all models pass is NOT a problem (that is the alarm
                working), so floor tests are never flagged for saturation.
    """
    saturated = []
    regressions = []
    for key, t in tests.items():
        layer = t["layer"]
        if layer == "discriminating":
            if t["n_models"] > 0 and t["n_passed"] == t["n_models"]:
                saturated.append(key)
        elif layer == "floor":
            for m, fails in t["fails_by_model"].items():
                runs = t["runs_by_model"].get(m, fails)
                regressions.append((m, key, fails, runs))
    saturated.sort()
    regressions.sort()
    return saturated, regressions


# ----------------------------------------------------------------------------
# Recommend: cheapest model that clears the bar
# ----------------------------------------------------------------------------

def _metric_field(optimize):
    """Map an --optimize choice to the aggregate field the selection sorts on."""
    return "latency_ms" if optimize == "latency" else "cost_per_test"


def optimize_sort_key(optimize):
    """Sort key over agg items for the active metric. Lower is better for both
    cost and latency. Models with an unknown metric value (e.g. local models
    that report no cost, or results with no latencyMs) sort last, since we
    cannot claim they are cheapest/fastest without a number."""
    field = _metric_field(optimize)

    def key(item):
        v = item[1].get(field)
        return (v is None, v if v is not None else 0.0)
    return key


def recommend(agg, bar, disc_bar, optimize="cost"):
    def clears(s):
        if s["floor_rate"] is None or s["floor_rate"] < bar:
            return False
        if disc_bar > 0 and (s["disc"] is None or s["disc"] < disc_bar):
            return False
        return True

    passers = [(m, s) for m, s in agg.items() if clears(s)]
    if not passers:
        return None
    # The "clears the bar" gate above is always the floor pass-rate vs --bar.
    # The chosen metric (cost or latency) is only the tiebreak/optimization
    # AMONG models that already clear it. Cheapest (cost) or fastest by median
    # latency (latency) wins.
    passers.sort(key=optimize_sort_key(optimize))
    return passers[0][0]


# ----------------------------------------------------------------------------
# Per-category routing: split the records by category, aggregate each category
# with the SAME rules as the overall aggregate, then pick the cheapest model
# that clears the bar WITHIN each category. The result is a routing policy:
# category -> model (Opus for the hard categories, a cheap model for the easy
# ones). For a multi-repo project each service is a category, so this is
# per-service model routing.
# ----------------------------------------------------------------------------

def aggregate_by_category(records):
    """Return {category: per-model-aggregate} plus whether anything is categorized.

    Groups records by rec_category (None -> "uncategorized"), then reuses
    aggregate() on each group so floor pass-rate, discriminating mean, and cost
    per test are computed by identical rules to the overall view.
    """
    categorized = any(rec_category(r) is not None for r in records)
    groups = {}
    for r in records:
        cat = rec_category(r) or "uncategorized"
        groups.setdefault(cat, []).append(r)
    out = {}
    for cat, recs in groups.items():
        agg, _layered = aggregate(recs)
        out[cat] = agg
    return out, categorized


def route_by_category(cat_aggs, bar, disc_bar, optimize="cost"):
    """category -> (recommended model or None, cost_per_test or None, latency_s or None).

    Reuses recommend() so each category uses the same bar / disc_bar gate and
    the same optimize metric as the overall recommendation. A None model means
    no model cleared the bar within that category. Both cost and latency are
    returned when known, regardless of which metric drove the selection, so the
    routing table can show either column.
    """
    routing = {}
    for cat, agg in cat_aggs.items():
        rec = recommend(agg, bar, disc_bar, optimize)
        cpt = agg[rec]["cost_per_test"] if (rec and rec in agg) else None
        lat_s = agg[rec]["latency_s"] if (rec and rec in agg) else None
        routing[cat] = (rec, cpt, lat_s)
    return routing


def dual_routing(cat_aggs, bar, disc_bar):
    """{category: {"cheapest": (model, cpt, lat_s), "fastest": (model, cpt, lat_s)}}.

    Runs the SAME route selection once per metric. The set of models that CLEAR
    the bar is identical for both lenses (the gate is the floor pass-rate, which
    is metric independent); only the tiebreak differs. So cheapest and fastest
    can name DIFFERENT models when more than one model clears within a category,
    and always name the same model when exactly one clears (or none).
    """
    cost = route_by_category(cat_aggs, bar, disc_bar, "cost")
    lat = route_by_category(cat_aggs, bar, disc_bar, "latency")
    return {c: {"cheapest": cost[c], "fastest": lat[c]} for c in cat_aggs}


# ----------------------------------------------------------------------------
# Category labels (optional sidecar) + owner-summary derivation
# ----------------------------------------------------------------------------

def load_category_labels(results_path):
    """Read an OPTIONAL <results-dir>/categories.yaml sidecar (same schema the
    digest uses: categories: {name: {label, graded_on}}).

    Returns {category-name: label}. Absent file, no PyYAML, unreadable file, or
    malformed content yields {} (never raises), so a missing sidecar just means
    the report falls back to the bare category name. This ties the report to the
    digest's categories without making PyYAML a hard dependency.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(results_path)),
                        "categories.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    cats = (d or {}).get("categories") if isinstance(d, dict) else None
    if not isinstance(cats, dict):
        return {}
    out = {}
    for name, entry in cats.items():
        if isinstance(entry, dict) and entry.get("label"):
            out[str(name).lower()] = str(entry["label"])
    return out


def category_label(cat, labels):
    """The human label for a category, falling back to the bare name."""
    return (labels or {}).get(cat, cat)


def strongest_model(agg):
    """The model CLOSEST to clearing when none does: highest floor pass-rate,
    tie-broken by higher discriminating score, then any model. Returns None for
    an empty aggregate."""
    best = None
    for m, s in agg.items():
        fr = s.get("floor_rate")
        fr = -1.0 if fr is None else fr
        dsc = s.get("disc")
        dsc = -1.0 if dsc is None else dsc
        key = (fr, dsc, m)
        if best is None or key > best[0]:
            best = (key, m)
    return best[1] if best else None


def category_route_info(cat, agg, bar, disc_bar, optimize):
    """Everything the owner line for one category needs.

    Returns dict: model (recommended, or None), n_clearers, strongest (closest
    model when none clears), reason (plain sentence fragment), cpt, lat_s.
    Reasons, none hardcoding a model name:
      "the only model that clears the bar"        exactly one model clears
      "fastest that clears the bar" / "cheapest that clears the bar"
                                                  more than one clears
      "no model clears the bar yet, <strongest> is closest, review the checks"
                                                  nothing clears
    """
    def clears(s):
        if s["floor_rate"] is None or s["floor_rate"] < bar:
            return False
        if disc_bar > 0 and (s["disc"] is None or s["disc"] < disc_bar):
            return False
        return True

    clearers = [m for m, s in agg.items() if clears(s)]
    model = recommend(agg, bar, disc_bar, optimize)
    cpt = agg[model]["cost_per_test"] if (model and model in agg) else None
    lat_s = agg[model]["latency_s"] if (model and model in agg) else None
    if model is None:
        strong = strongest_model(agg)
        reason = ("no model clears the bar yet, " + str(strong)
                  + " is closest, review the checks")
        return {"model": None, "n_clearers": 0, "strongest": strong,
                "reason": reason, "cpt": None, "lat_s": None}
    if len(clearers) == 1:
        reason = "the only model that clears the bar"
    else:
        reason = ("fastest that clears the bar" if optimize == "latency"
                  else "cheapest that clears the bar")
    return {"model": model, "n_clearers": len(clearers), "strongest": model,
            "reason": reason, "cpt": cpt, "lat_s": lat_s}


def owner_routing(cat_aggs, bar, disc_bar, optimize="cost"):
    """category -> category_route_info, for every category, sorted-friendly."""
    return {cat: category_route_info(cat, agg, bar, disc_bar, optimize)
            for cat, agg in cat_aggs.items()}


def everyday_pick(owner_route, optimize, agg):
    """The model recommended for the MOST categories (the 'everyday' pick).

    Ties are broken by the overall optimize metric (the cheaper / faster model
    overall wins the 'everyday' label). Returns the model id, or None when no
    category routes to any model.
    """
    counts = {}
    for info in owner_route.values():
        m = info["model"]
        if m is not None:
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return None
    field = _metric_field(optimize)

    def tiebreak(m):
        v = agg.get(m, {}).get(field)
        return (v is None, v if v is not None else 0.0)

    best = max(counts, key=lambda m: (counts[m], -tiebreak(m)[0],
                                      -(tiebreak(m)[1])))
    return best


def category_floor_failures(records):
    """{category: [(model, test, fails, runs), ...]} DEDUPED per (model, test).

    Groups records by category, then reuses aggregate_tests + suite_health on
    each group, so a floor test that fails on every one of K repeats appears once
    as (model, test, K, K), never K times.
    """
    groups = {}
    for r in records:
        c = rec_category(r) or "uncategorized"
        groups.setdefault(c, []).append(r)
    out = {}
    for c, recs in groups.items():
        _saturated, regressions = suite_health(aggregate_tests(recs))
        out[c] = regressions
    return out


# ----------------------------------------------------------------------------
# Text report
# ----------------------------------------------------------------------------

def fmt_rate(x):
    return "n/a" if x is None else "{:.0f}%".format(x * 100)


def fmt_score(x):
    return "n/a" if x is None else "{:.2f}".format(x)


def fmt_cost(x):
    # Per-test cost is fractions of a cent and unreadable at 5-6 decimals, so we
    # display it per 100 tests, a readable dollar figure. Self-describing ("/100")
    # because it is also used inline where there is no column header. Absolute run
    # totals (the run-cost panel) use their own dollar formatter, not this one.
    if x is None:
        return "n/a"
    if x == 0:
        return "$0 (free)"
    per_c = x * 100.0
    if per_c < 0.01:
        return "<$0.01/100"
    return "${:,.2f}/100".format(per_c)


def fmt_latency(x):
    """Median latency in seconds (input is already seconds)."""
    if x is None:
        return "n/a"
    return "{:.3f}".format(x).rstrip("0").rstrip(".") + "s"


def print_report(agg, layered, bar, disc_bar, rec_model_id, incumbent, optimize="cost"):
    field = _metric_field(optimize)
    rows = sorted(agg.items(), key=optimize_sort_key(optimize))
    name_w = max([len("model")] + [len(m) for m in agg]) + 2
    # Always show cost/test when known; the active metric gets its own column.
    header = "{:<{w}} {:>10} {:>8} {:>14} {:>14}".format(
        "model", "floor", "disc", "cost/100", "latency", w=name_w)
    print(header)
    print("-" * len(header))
    for m, s in rows:
        cpt = s["cost_per_test"]
        mark = ""
        if m == rec_model_id:
            mark = "  <- recommended"
        elif incumbent and m == incumbent:
            mark = "  (you are here)"
        print("{:<{w}} {:>10} {:>8} {:>14} {:>14}{}".format(
            m, fmt_rate(s["floor_rate"]), fmt_score(s["disc"]),
            fmt_cost(cpt), fmt_latency(s["latency_s"]), mark, w=name_w))
    print()
    if not layered:
        print("Note: tests were not tagged by layer, so floor = overall pass-rate "
              "and disc = mean score across all tests.")
    metric_word = "fastest by median latency" if optimize == "latency" else "lowest cost"
    if rec_model_id:
        s = agg[rec_model_id]
        print("Run: {}. Clears the bar (floor {} >= {:.0f}%){} at the {}."
              .format(rec_model_id, fmt_rate(s["floor_rate"]), bar * 100,
                      "" if disc_bar <= 0 else ", disc {} >= {:.2f}".format(
                          fmt_score(s["disc"]), disc_bar),
                      metric_word))
    else:
        print("No model clears the bar (floor pass-rate >= {:.0f}%{}). "
              "Raise coverage, lower the bar, or add a stronger model."
              .format(bar * 100,
                      "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
    if incumbent and rec_model_id and incumbent in agg and incumbent != rec_model_id:
        inc = agg[incumbent].get(field)
        rec = agg[rec_model_id].get(field)
        if inc is not None and rec is not None and inc > 0:
            save = (1 - rec / inc) * 100
            better = "faster" if optimize == "latency" else "cheaper"
            unit = "per test" if optimize == "cost" else "in median latency"
            print("Versus your current {}: about {:.0f}% {} {} at or above your bar."
                  .format(incumbent, save, better, unit))


def print_health(tests, layered):
    print()
    title = "Suite health"
    print(title)
    print("-" * len(title))
    if not layered:
        print("Tests were not tagged by layer, so no saturation / regression "
              "checks were run (they need floor / discriminating tags).")
        return
    saturated, regressions = suite_health(tests)
    if not saturated and not regressions:
        print("No issues: no saturated discriminating tests, and every model "
              "cleared every floor test.")
        return
    for m, test, fails, runs in regressions:
        print('FLOOR FAIL: model {} fails "{}" ({}/{} runs): regression or '
              'coverage gap.'.format(m, test, fails, runs))
    for test in saturated:
        print('SATURATED: discriminating test "{}" - all models passed, so it '
              'gives no ranking signal. Harden or retire it.'.format(test))


def print_routing(cat_aggs, categorized, bar, disc_bar, optimize="cost"):
    print()
    title = "Per-category routing"
    print(title)
    print("-" * len(title))
    if not categorized:
        print("No test carried a category, so per-category routing is absent. "
              "Tag tests with metadata.category to enable it.")
        return
    routing = route_by_category(cat_aggs, bar, disc_bar, optimize)
    cats = sorted(routing)
    cat_w = max([len("category")] + [len(c) for c in cats]) + 2
    mdl_w = max([len("model")]
                + [len(t[0]) for t in routing.values() if t[0]]) + 2
    header = "{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
        "category", "model", "cost/100", "latency", cw=cat_w, mw=mdl_w)
    print(header)
    print("-" * len(header))
    for c in cats:
        model, cpt, lat_s = routing[c]
        if model is None:
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, "NO MODEL CLEARS THE BAR", "-", "-", cw=cat_w, mw=mdl_w))
        else:
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, model, fmt_cost(cpt), fmt_latency(lat_s), cw=cat_w, mw=mdl_w))
    print()
    pick = ("fastest by median latency that clears the bar, "
            if optimize == "latency" else "cheapest that clears the bar, ")
    print("Routing policy: send each category to the model shown ({}floor >= {:.0f}%{})."
          .format(pick, bar * 100,
                  "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
    unmet = [c for c in cats if routing[c][0] is None]
    if unmet:
        print("No model clears the bar in: {}. Raise coverage, lower the bar, "
              "or add a stronger model for these.".format(", ".join(unmet)))


# ----------------------------------------------------------------------------
# HTML report with inline SVG frontier (no external libraries)
# ----------------------------------------------------------------------------

def _frontier_ylo(agg):
    """Lower bound (in percent) for the zoomed floor-pass-rate axis.

    min(80, floor(lowest model pass-rate in percent)). So a suite whose worst
    model sits at 94% gets an 80-to-100 axis (dots spread out instead of
    crushing against the top), while a genuinely weak 71% gets a 71-to-100 axis.
    The bound NEVER starts above 80, so a strong suite always keeps 20 points of
    visible headroom. Returns 0 only in the degenerate all-unknown case.
    """
    rates = [s["floor_rate"] for s in agg.values() if s.get("floor_rate") is not None]
    if not rates:
        return 0
    import math
    return min(80, int(math.floor(min(rates) * 100)))


# Dot palette (adopted from the Broadsheet design's print-ink ramps): color a dot
# by VENDOR FAMILY and shade it by floor within the family (best = darkest ink).
# The free / local tier takes the neutral GRAY ramp, so the weakest models recede
# exactly as in the design. Greens / purples stay reserved for the ring markers.
_FAMILY_RAMPS = {
    "anthropic": ["#790e3d", "#aa0b56", "#d82071", "#ff458e", "#ff90b1"],  # magenta ink
    "cyan":      ["#004961", "#006786", "#1186ac", "#38a6cf", "#62c5ee"],  # cyan / blue (OpenAI + Google)
    "local":     ["#444141", "#605d5d", "#7d7979", "#9b9797"],             # neutral gray (free / local)
    "other":     ["#8a6d00", "#b8890f", "#c99a1a", "#edbb00"],             # process yellow (any other vendor)
}
_GROUP_GRAY = "#9b9797"   # the collapsed "+N more" group dot

_WIN_RING = "#1a7f37"   # green ring = the pick for THIS chart's metric
_INC_RING = "#8250df"   # purple dashed ring = incumbent, "you are here"

# Approximate list prices ($ per 1M input, $ per 1M output) for models we know,
# used ONLY to estimate GRADING spend from the judge's grading tokens: promptfoo
# prices generation itself but does not price grading. Provider/generation cost
# always comes from promptfoo's own per-record figure, so a missing entry here
# never affects it; it only means the judge's grading cost shows "not auto-priced".
# Keyed by a substring of the model id. Update when prices change.
_PRICE_PER_M = {
    "claude-opus-4-8": (5, 25), "claude-opus-5": (5, 25),
    "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25),
    "claude-sonnet-5": (3, 15), "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5), "claude-fable-5": (10, 50),
    "gpt-6-astra": (10, 50),
}


def _model_family(model, s):
    """Which ink ramp a model draws from. Local / free first (they read gray),
    then vendor by id prefix; anything else falls to the 'other' ramp."""
    mid = (model or "").lower()
    if ("ollama" in mid or "localhost" in mid or "127.0.0.1" in mid
            or "lm-studio" in mid or s.get("cost_per_test") == 0):
        return "local"
    if mid.startswith("anthropic") or "claude" in mid:
        return "anthropic"
    if (mid.startswith("openai") or "gpt" in mid or "o1" in mid or "o3" in mid
            or mid.startswith("google") or "gemini" in mid):
        return "cyan"
    return "other"


def _model_colors(agg):
    """{model: color}. Grouped by vendor family, shaded by floor within the family
    (best = darkest ink). Local / free models take the neutral gray ramp. Same map
    is used by both frontier charts and the shared legend, so a model's color is
    identical everywhere."""
    fam = {}
    for m, s in agg.items():
        fam.setdefault(_model_family(m, s), []).append((m, s))
    colors = {}
    for f, members in fam.items():
        ramp = _FAMILY_RAMPS.get(f, _FAMILY_RAMPS["other"])
        members.sort(key=lambda ms: (ms[1].get("floor_rate") is None,
                                     -(ms[1].get("floor_rate") or 0.0), ms[0]))
        for i, (m, _s) in enumerate(members):
            colors[m] = ramp[min(i, len(ramp) - 1)]
    return colors


def _frontier_rank(items):
    """Rank (model, stats) pairs by overall quality: floor desc, then disc desc,
    then id. The single ordering the charts, the group split, and the legend all
    share so dot identity follows the table's floor-descending order."""
    return sorted(items, key=lambda ms: (ms[1].get("floor_rate") is None,
                                          -(ms[1].get("floor_rate") or 0.0),
                                          -(ms[1].get("disc") or 0.0), ms[0]))


def _frontier_split(items, top_n):
    """Top-N shown individually; the rest collapse to one group whose anchor is the
    BEST remainder (so the group dot sits at a real coordinate). Only collapses when
    it would fold at least TWO models - folding a single one into '+1 more' is
    pointless, so in that case everything is shown."""
    ranked = _frontier_rank(items)
    if top_n and top_n > 0 and len(ranked) > top_n + 1:
        return ranked[:top_n], ranked[top_n:], ranked[top_n]
    return ranked, [], None


def _svg_frontier_plot(agg, colors, metric, ringed, incumbent, ylo, top_n=None):
    """One plot-only frontier SVG (no legend; the legend is shared across both
    charts and rendered once beside them).

    metric    'cost' -> x is cost per test; 'latency' -> x is median latency (s).
    ringed    the model to green-ring as the pick for THIS metric (or None).
    incumbent the model to mark with the purple dashed "you are here" ring.
    ylo       the shared zoomed y-axis floor (percent) so BOTH charts use one
              identical floor-pass-rate scale (min(80, lowest rate)..100).
    Dots are colored by the shared `colors` map (model identity), so the same
    model is the same color in both charts.
    """
    latency = (metric == "latency")
    field = "latency_s" if latency else "cost_per_test"
    axis_word = "median latency" if latency else "cost"
    pts = [(m, s) for m, s in agg.items()
           if s.get(field) is not None and s.get("floor_rate") is not None]
    W, H = 430, 330
    ml, mr, mt, mb = 56, 18, 16, 54
    pw, ph = W - ml - mr, H - mt - mb
    if not pts:
        return ('<svg width="{w}" height="80"><text x="10" y="45" '
                'font-family="system-ui" font-size="13">No models reported a {a}; '
                'nothing to plot on the {a} axis.</text></svg>').format(w=W, a=axis_word)
    # Rank by floor and split into the top_n shown individually plus a collapsed
    # gray group anchored at the best remainder. max_x fits only the DRAWN dots.
    shown, grouped, anchor = _frontier_split(pts, top_n)
    drawn = shown + ([anchor] if anchor else [])
    max_x = (max(s[field] for _, s in drawn) or 1e-9) * 1.15
    ylo_f = ylo / 100.0               # fraction
    span = (1.0 - ylo_f) or 1e-9

    def px(c):
        return ml + (c / max_x) * pw

    def py(rate):
        r = ylo_f if rate is None else rate
        r = max(ylo_f, min(1.0, r))
        return mt + (1 - (r - ylo_f) / span) * ph

    parts = ['<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
             'font-family="system-ui, sans-serif">'.format(w=W, h=H)]
    parts.append('<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>'.format(w=W, h=H))
    # axes
    parts.append('<line x1="{x}" y1="{t}" x2="{x}" y2="{b}" stroke="#888"/>'.format(
        x=ml, t=mt, b=mt + ph))
    parts.append('<line x1="{x}" y1="{b}" x2="{r}" y2="{b}" stroke="#888"/>'.format(
        x=ml, b=mt + ph, r=ml + pw))
    # y grid + labels (zoomed pass-rate ylo..100), shared across both charts
    for i in range(0, 5):
        pct = ylo + (100 - ylo) * i / 4.0
        y = py(pct / 100.0)
        parts.append('<line x1="{x}" y1="{y}" x2="{r}" y2="{y}" stroke="#eef0f4"/>'.format(
            x=ml, y=y, r=ml + pw))
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#667" '
                     'text-anchor="end">{v:.0f}%</text>'.format(x=ml - 8, y=y + 4, v=pct))
    # x labels (this chart's metric: cost per test, or median latency in seconds)
    for i in range(0, 5):
        c = max_x * i / 4.0
        x = px(c)
        lbl = "{v:.3f}s".format(v=c) if latency else "${v:,.2f}".format(v=c * 100)
        parts.append('<text x="{x}" y="{y}" font-size="10" fill="#667" '
                     'text-anchor="middle">{l}</text>'.format(x=x, y=mt + ph + 16, l=lbl))
    axis_title = ("median latency per test (s), lower is better" if latency
                  else "cost per 100 tests (USD), lower is better")
    parts.append('<text x="{x}" y="{y}" font-size="11" fill="#333" '
                 'text-anchor="middle">{t}</text>'.format(
                     x=ml + pw / 2, y=H - 8, t=html.escape(axis_title)))
    parts.append('<text transform="translate(14,{y}) rotate(-90)" font-size="11" '
                 'fill="#333" text-anchor="middle">floor pass-rate</text>'.format(
                     y=mt + ph / 2))
    def xlab(v):
        return fmt_latency(v) if latency else fmt_cost(v)

    # Top-N shown individually, colored by vendor ink. Incumbent gets a purple
    # dashed ring; the pick for THIS chart's metric a green ring; a model can carry
    # both. data-model drives cross-highlight with the legend and table.
    for m, s in shown:
        x, y = px(s[field]), py(s["floor_rate"])
        parts.append(
            '<g class="cw-dot" data-model="{m}" data-floor="{fr}" '
            'data-metric="{mw}" data-xlabel="{xl}">'.format(
                m=html.escape(m), fr=html.escape(fmt_rate(s["floor_rate"])),
                mw=("latency" if latency else "cost"), xl=html.escape(xlab(s[field]))))
        if incumbent and m == incumbent:
            parts.append('<circle cx="{x}" cy="{y}" r="10" fill="none" '
                         'stroke="{c}" stroke-width="2" stroke-dasharray="3 2"/>'.format(
                             x=x, y=y, c=_INC_RING))
        if m == ringed:
            parts.append('<circle cx="{x}" cy="{y}" r="12" fill="none" '
                         'stroke="{c}" stroke-width="2.5"/>'.format(x=x, y=y, c=_WIN_RING))
        parts.append('<circle class="dot" cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(
                         x=x, y=y, c=colors.get(m, "#0969da")))
        parts.append('</g>')
    # The collapsed remainder: ONE gray dot at the best remainder's real (x, y),
    # with an always-on "+N" label. Its data-model is the group key so it
    # cross-highlights with its legend entry.
    if anchor:
        am, asx = anchor
        gx, gy = px(asx[field]), py(asx["floor_rate"])
        gkey = "+{} more".format(len(grouped))
        parts.append(
            '<g class="cw-dot" data-model="{k}" data-floor="{fr}" data-metric="{mw}" '
            'data-xlabel="{xl}">'.format(
                k=html.escape(gkey), fr=html.escape(fmt_rate(asx["floor_rate"])),
                mw=("latency" if latency else "cost"), xl=html.escape(xlab(asx[field]))))
        parts.append('<circle class="dot" cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(x=gx, y=gy, c=_GROUP_GRAY))
        parts.append('<text x="{x}" y="{y}" dy="-9" font-size="10" text-anchor="middle" '
                     'fill="#667">{k}</text>'.format(x=gx, y=gy, k=html.escape(gkey)))
        parts.append('</g>')
    parts.append("</svg>")
    return "".join(parts)


def _frontier_legend_html(agg, colors, rec_cost, rec_lat, incumbent, top_n=None):
    """The single shared legend for BOTH frontier charts: one row per model with
    its color swatch (same color used in both charts) and BOTH metrics, plus a
    marker key. Rendered once, not duplicated per chart."""
    esc = html.escape
    shown, grouped, _anchor = _frontier_split(list(agg.items()), top_n)
    parts = ['<div class="frontier-legend"><div class="fl-models">']
    for m, s in shown:
        tags = []
        if m == rec_cost:
            tags.append("cheapest")
        if m == rec_lat:
            tags.append("fastest")
        if incumbent and m == incumbent:
            tags.append("you are here")
        tagtxt = (' <span class="fl-tag">(' + ", ".join(tags) + ")</span>") if tags else ""
        parts.append(
            '<span class="legend-item" data-model="{m}"><span class="swatch" '
            'style="background:{c}"></span><b>{m}</b>{tag}</span>'.format(
                c=colors.get(m, "#0969da"), m=esc(m), tag=tagtxt))
    if grouped:
        gkey = "+{} more".format(len(grouped))
        names = ", ".join(g[0].split(":")[-1] for g in grouped)
        parts.append(
            '<span class="legend-item" data-model="{k}"><span class="swatch" '
            'style="background:{c}"></span><b>{k} models</b> '
            '<span class="fl-tag" style="color:#889">({names})</span></span>'.format(
                k=esc(gkey), c=_GROUP_GRAY, names=esc(names)))
    # Only advertise a ring when it is actually drawn: the green (pick) ring only
    # exists when some model clears the bar, and the purple (incumbent) ring only
    # when an incumbent was passed. Mentioning an absent ring reads as a bug.
    hint = ('hover or click a model to highlight it in both charts '
            '(metrics are in the table below)')
    key_bits = []
    if rec_cost or rec_lat:
        key_bits.append('<span class="k-ring win"></span> green ring = pick for that chart')
    if incumbent:
        key_bits.append('<span class="k-ring inc"></span> purple dashed ring = you are here')
    if key_bits:
        keyhtml = '&nbsp;&nbsp; '.join(key_bits) + '&nbsp;&nbsp; ' + hint
    else:
        keyhtml = 'no model cleared the bar, so no pick is ringed. ' + hint
    parts.append('</div><div class="fl-key">' + keyhtml + '</div></div>')
    return "".join(parts)


_REPORT_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1f2b; background: #f7f8fa; margin: 0; padding: 2rem 1rem; line-height: 1.5; }
.wrap { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
h2 { font-size: 1.05rem; margin: 1.8rem 0 .6rem; color: #2a3140; }
.owner { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: 1rem 1.15rem; margin: 0 0 1rem; }
.owner .headline { font-size: 1.05rem; color: #1a1f2b; margin: 0 0 .7rem; }
.owner .headline b { color: #0f5a2a; }
.owner ul { margin: .3rem 0 0; padding: 0; list-style: none; }
.owner li { font-size: .95rem; color: #333; padding: .28rem 0;
  border-top: 1px solid #eef0f4; }
.owner li:first-child { border-top: none; }
.owner li .cat { font-weight: 600; }
.owner li .mdl { color: #0f5a2a; font-weight: 600; }
.owner li .why { color: #778; font-size: .86rem; }
.owner li.none .mdl { color: #b35900; }
.note { color: #8a6d00; font-size: .86rem; margin: .4rem 0; }
.chart { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: .6rem; overflow-x: auto; }
.chart svg { display: block; max-width: 100%; height: auto; margin: 0 auto; }
.chart-title { font-size: .85rem; font-weight: 600; color: #2a3140;
  text-align: center; padding: .1rem 0 .2rem; }
.frontier-row { display: flex; flex-wrap: wrap; gap: .7rem; }
.frontier-row > .chart { flex: 1 1 360px; min-width: 280px; }
.frontier-legend { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: .6rem .8rem; margin: .2rem 0 .6rem; }
.frontier-legend .fl-models { display: flex; flex-wrap: wrap; gap: .45rem 1.1rem; }
.legend-item { font-size: .84rem; color: #222; display: inline-flex; align-items: center; }
.legend-item .swatch { width: 11px; height: 11px; border-radius: 50%; display: inline-block;
  margin-right: .35rem; box-shadow: 0 0 0 1px #ccd; }
.legend-item .fl-metrics { color: #778; margin-left: .35rem; }
.legend-item .fl-tag { color: #1a7f37; font-weight: 600; }
.fl-key { font-size: .78rem; color: #889; margin-top: .55rem; }
.fl-key .k-ring { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
  vertical-align: middle; margin-right: .2rem; }
.fl-key .k-ring.win { border: 2px solid #1a7f37; }
.fl-key .k-ring.inc { border: 2px dashed #8250df; }
table.routing { border-collapse: collapse; font-size: .88rem; width: 100%; margin: .3rem 0 .6rem; }
table.routing th { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; }
table.routing td { padding: .4rem .6rem; border-bottom: 1px solid #f0f2f6; vertical-align: top; }
table.routing td .mdl { color: #0f5a2a; font-weight: 600; }
table.routing td .rt-metric { color: #778; font-size: .84rem; }
table.routing td .rt-note { color: #8250df; font-size: .8rem; font-weight: 600; }
table.routing tr.none td { color: #b35900; }
.lens-note { color: #667; font-size: .84rem; margin: .2rem 0 .3rem; }
.disagree-note { color: #8a6d00; font-size: .88rem; margin: .35rem 0; }
.agree-note { color: #4a8a5a; font-size: .84rem; margin: .35rem 0; }
table.models { border-collapse: collapse; font-size: .86rem; width: 100%; margin: .2rem 0; }
table.models th { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; white-space: nowrap; }
table.models th.l, table.models td.l { text-align: left; }
table.models td { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #f0f2f6;
  white-space: nowrap; }
table.models tr.win td { background: #f0faf3; }
table.models td .win-tag { color: #1a7f37; font-weight: 700; font-size: .78rem; }
table.models td .here-tag { color: #8250df; font-weight: 700; font-size: .78rem; }
details.cat { border: 1px solid #d7dce4; border-radius: 8px; background: #fff;
  margin-bottom: .7rem; overflow: hidden; }
details.cat > summary { font-size: 1rem; padding: .7rem .9rem; cursor: pointer;
  background: #fbfcfe; list-style: none; }
details.cat > summary::-webkit-details-marker { display: none; }
details.cat > summary::before { content: "\\25B8"; color: #99a; margin-right: .5rem; }
details.cat[open] > summary::before { content: "\\25BE"; }
details.cat > summary .cat { font-weight: 600; }
details.cat > summary .arrow { color: #889; }
details.cat > summary .mdl { color: #0f5a2a; font-weight: 600; }
details.cat > summary .why { color: #778; font-size: .82rem; margin-left: .3rem; }
details.cat[data-none="1"] > summary .mdl { color: #b35900; }
.cat-body { padding: .3rem .9rem .9rem; }
.fail-head { font-weight: 600; color: #b35900; font-size: .84rem; margin: .8rem 0 .3rem; }
ul.fails { margin: .2rem 0 .2rem 1.1rem; padding: 0; }
ul.fails li { font-size: .86rem; color: #333; margin: .18rem 0; }
ul.fails li b { color: #1a1f2b; }
.clean { color: #1a7f37; font-size: .86rem; margin: .5rem 0 .2rem; }
ul.sat { margin: .2rem 0 .2rem 1.1rem; padding: 0; }
ul.sat li { font-size: .86rem; color: #333; margin: .18rem 0; }
.foot { color: #889; font-size: .8rem; margin-top: 1.8rem; }
.controls { position: fixed; top: 12px; right: 14px; z-index: 20; display: flex; gap: 6px; }
.controls button { font: inherit; font-size: .78rem; padding: .35rem .7rem; cursor: pointer;
  border: 1px solid #cdd3dd; background: #fff; border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.controls button:hover { background: #eef1f6; }
/* interactivity: cross-highlight (legend <-> both charts <-> table) + tooltip + sort */
.legend-item { cursor: pointer; padding: .12rem .3rem; border-radius: 5px;
  transition: opacity .1s, background .1s; }
.legend-item.cw-on { background: #eef4ff; }
.cw-dot { cursor: pointer; }
.cw-dot circle.dot { transition: opacity .1s; }
body.cw-hl .cw-dot:not(.cw-on) { opacity: .15; }
body.cw-hl .legend-item:not(.cw-on) { opacity: .32; }
body.cw-hl table.models tbody tr:not(.cw-on) { opacity: .34; }
.cw-dot.cw-on circle.dot { stroke: #1a1f2b; stroke-width: 2.5; }
table.models tbody tr { cursor: pointer; }
table.models tbody tr.cw-on { outline: 2px solid #0969da; outline-offset: -2px; }
.cw-tip { position: fixed; z-index: 50; background: #1a1f2b; color: #fff;
  font-size: .76rem; padding: .38rem .55rem; border-radius: 6px; pointer-events: none;
  line-height: 1.4; box-shadow: 0 2px 10px rgba(0,0,0,.28); max-width: 260px; }
.sort-hint { font-size: .78rem; color: #889; margin: .1rem 0 .45rem; }
table.models th.sortable { cursor: pointer; user-select: none; }
table.models th.sortable:hover { color: #0969da; }
table.models th.sortable::after { content: " \\2195"; color: #c4cbd6; font-weight: 400; }
table.models th.sortable.sorted[data-dir="desc"]::after { content: " \\25BC"; color: #0969da; }
table.models th.sortable.sorted[data-dir="asc"]::after { content: " \\25B2"; color: #0969da; }
table.runcost { border-collapse: collapse; font-size: .84rem; width: 100%; margin: .3rem 0 .5rem; }
table.runcost th { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; white-space: nowrap; }
table.runcost th.l, table.runcost td.l { text-align: left; }
table.runcost td { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #f0f2f6;
  white-space: nowrap; }
table.runcost tr.subtotal td, table.runcost tr.total td { font-weight: 700;
  border-top: 2px solid #dde2ea; background: #f8fafc; }
table.runcost tr.grading td { color: #6b3fb0; }
.runcost-head { font-size: .95rem; color: #1a1f2b; margin: .2rem 0 .55rem; }
.runcost-head b { color: #0f5a2a; }
.runcost-note { font-size: .78rem; color: #889; margin: .35rem 0; }
"""


# Interactivity, kept out of the .format() template so its many braces need no
# escaping. Injected as {script}. cwAll = expand/collapse; the IIFE wires
# cross-highlight (legend <-> both charts <-> table), the dot tooltip, and the
# sortable all-models table. Cross-highlight keys on data-model, present on every
# legend item, chart dot (as <g class="cw-dot">), and table row.
_REPORT_JS = """
function cwAll(o){document.querySelectorAll('details').forEach(function(d){d.open=o;});}
(function(){
  var pinned=null;
  function nodesFor(model){return Array.prototype.filter.call(document.querySelectorAll('[data-model]'),function(e){return e.getAttribute('data-model')===model;});}
  function clearHi(){document.body.classList.remove('cw-hl');Array.prototype.forEach.call(document.querySelectorAll('.cw-on'),function(e){e.classList.remove('cw-on');});}
  function setHi(model){clearHi();document.body.classList.add('cw-hl');nodesFor(model).forEach(function(e){e.classList.add('cw-on');});}
  Array.prototype.forEach.call(document.querySelectorAll('[data-model]'),function(el){
    var model=el.getAttribute('data-model');
    el.addEventListener('mouseenter',function(){if(!pinned)setHi(model);});
    el.addEventListener('mouseleave',function(){if(!pinned)clearHi();});
    el.addEventListener('click',function(){if(pinned===model){pinned=null;clearHi();}else{pinned=model;setHi(model);}});
  });
  var tip=document.createElement('div');tip.className='cw-tip';tip.style.display='none';document.body.appendChild(tip);
  function show(d,e){tip.innerHTML='<b>'+d.getAttribute('data-model')+'</b><br>floor '+d.getAttribute('data-floor')+' \\u00b7 '+d.getAttribute('data-metric')+' '+d.getAttribute('data-xlabel');tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';}
  Array.prototype.forEach.call(document.querySelectorAll('.cw-dot'),function(d){
    d.addEventListener('mouseenter',function(e){show(d,e);});
    d.addEventListener('mousemove',function(e){show(d,e);});
    d.addEventListener('mouseleave',function(){tip.style.display='none';});
  });
  Array.prototype.forEach.call(document.querySelectorAll('table.models th.sortable'),function(th){
    th.addEventListener('click',function(){
      var tb=th.closest('table').querySelector('tbody'),idx=+th.getAttribute('data-col'),better=th.getAttribute('data-better'),cur=th.getAttribute('data-dir');
      var dir=cur?(cur==='asc'?'desc':'asc'):(better==='hi'?'desc':'asc');
      Array.prototype.forEach.call(th.parentNode.children,function(o){o.removeAttribute('data-dir');o.classList.remove('sorted');});
      th.setAttribute('data-dir',dir);th.classList.add('sorted');
      var rows=Array.prototype.slice.call(tb.querySelectorAll('tr'));
      rows.sort(function(a,b){var av=parseFloat(a.children[idx].getAttribute('data-sort')),bv=parseFloat(b.children[idx].getAttribute('data-sort'));if(isNaN(av))av=-1e15;if(isNaN(bv))bv=-1e15;return dir==='asc'?av-bv:bv-av;});
      rows.forEach(function(r){tb.appendChild(r);});
    });
  });
})();
"""


def _model_table(agg, winner, incumbent):
    """Compact per-model table: floor pass-rate, discriminating score, latency,
    cost, with the winner (recommended for this scope) and incumbent marked."""
    esc = html.escape
    rows = sorted(agg.items(), key=lambda kv: (kv[1]["floor_rate"] is None,
                                               -(kv[1]["floor_rate"] or 0)))
    out = ['<p class="sort-hint">Click a column to sort (best first; click again to '
           'flip). Hover a row to highlight that model in the charts.</p>'
           '<table class="models"><thead><tr>'
           '<th class="l">model</th>'
           '<th class="sortable" data-col="1" data-better="hi">floor</th>'
           '<th class="sortable" data-col="2" data-better="hi">disc</th>'
           '<th class="sortable" data-col="3" data-better="lo">latency</th>'
           '<th class="sortable" data-col="4" data-better="lo">cost /100</th>'
           '<th class="l"></th>'
           '</tr></thead><tbody>']
    for m, s in rows:
        is_win = (m == winner)
        is_here = (incumbent and m == incumbent)
        tag = ('<span class="win-tag">run this</span>' if is_win
               else ('<span class="here-tag">you are here</span>' if is_here else ""))
        fr_v, d_v, lat_v, c_v = (s["floor_rate"], s["disc"],
                                 s["latency_s"], s["cost_per_test"])
        out.append(
            '<tr class="{cls}" data-model="{m}"><td class="l">{m}</td>'
            '<td data-sort="{frs}">{fr}</td><td data-sort="{ds}">{d}</td>'
            '<td data-sort="{ls}">{lat}</td><td data-sort="{cs}">{c}</td>'
            '<td class="l">{tag}</td></tr>'.format(
                cls="win" if is_win else "", m=esc(m),
                frs=(fr_v if fr_v is not None else -1),
                ds=(d_v if d_v is not None else -1),
                ls=(lat_v if lat_v is not None else 1e15),
                cs=(c_v if c_v is not None else 1e15),
                fr=esc(fmt_rate(fr_v)), d=esc(fmt_score(d_v)),
                lat=esc(fmt_latency(lat_v)), c=esc(fmt_cost(c_v)), tag=tag))
    out.append("</tbody></table>")
    return "".join(out)


def _owner_summary_html(lead_route, route_cost, route_lat, everyday, labels, optimize):
    """Always-visible, plain-language routing summary an owner can read.

    Headline leads with the --optimize lens (`lead_route` is the routing for that
    lens): the everyday pick (recommended for the most categories) and the
    exceptions. Below it, a plain note surfaces every service where the CHEAPEST
    and FASTEST picks disagree (today they usually agree, so it will usually say
    they align). The per-category lines convey both lenses when they differ. No
    model name is hardcoded, so it reads naturally with any provider field.
    """
    esc = html.escape

    def lbl(c):
        return category_label(c, labels)

    lead_word = "fastest" if optimize == "latency" else "cheapest"
    parts = ['<div class="owner">']
    if everyday is None:
        parts.append('<p class="headline">No model clears the bar on any category '
                     'yet. Review the checks in each section below.</p>')
    else:
        everyday_cats = sorted(lbl(c) for c, i in lead_route.items()
                               if i["model"] == everyday)
        other = {}
        none_cats = []
        for c, i in lead_route.items():
            if i["model"] is None:
                none_cats.append(lbl(c))
            elif i["model"] != everyday:
                other.setdefault(i["model"], []).append(lbl(c))
        head = ('Run <b>{e}</b> for everyday work: {cats}.').format(
            e=esc(everyday), cats=esc(", ".join(everyday_cats)))
        sentences = [head]
        for m in sorted(other):
            sentences.append('Reach for <b>{m}</b> on {cats}.'.format(
                m=esc(m), cats=esc(", ".join(sorted(other[m])))))
        if none_cats:
            sentences.append('No model clears the bar yet on {cats}; '
                             'review the checks.'.format(
                                 cats=esc(", ".join(sorted(none_cats)))))
        if not other and not none_cats:
            sentences.append('It clears the bar on every categorized service.')
        parts.append('<p class="headline">' + " ".join(sentences) + "</p>")
        parts.append('<p class="lens-note">Headline follows your <b>{lw}</b> lens. '
                     'The two frontier charts and the per-service table below show '
                     'both cost and latency.</p>'.format(lw=esc(lead_word)))

    # Disagreement note: services where the cheapest and fastest clearer differ.
    disagree = []
    for c in route_cost:
        mc = route_cost[c]["model"] if isinstance(route_cost[c], dict) else route_cost[c][0]
        ml_ = route_lat[c]["model"] if isinstance(route_lat[c], dict) else route_lat[c][0]
        if mc is not None and ml_ is not None and mc != ml_:
            disagree.append((lbl(c), mc, ml_))
    routed_any = any((route_cost[c]["model"] if isinstance(route_cost[c], dict)
                      else route_cost[c][0]) is not None for c in route_cost)
    if disagree:
        frags = ["<b>{c}</b> (cheapest {a}, fastest {b})".format(
            c=esc(cl), a=esc(mc), b=esc(ml_)) for cl, mc, ml_ in sorted(disagree)]
        parts.append('<p class="disagree-note">Cheapest and fastest picks differ '
                     'on: ' + "; ".join(frags) + ".</p>")
    elif routed_any:
        parts.append('<p class="agree-note">On every service that clears the bar, '
                     'the cheapest and the fastest model are the same.</p>')

    parts.append("<ul>")
    for c in sorted(lead_route):
        i = lead_route[c]
        mc = route_cost[c]["model"]
        ml_ = route_lat[c]["model"]
        if i["model"] is None:
            parts.append(
                '<li class="none"><span class="cat">{cat}</span> '
                '<span class="arrow">-></span> <span class="mdl">no model clears '
                'it yet</span> <span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), why=esc(i["reason"])))
        elif mc is not None and ml_ is not None and mc != ml_:
            parts.append(
                '<li><span class="cat">{cat}</span> <span class="arrow">-></span> '
                'cheapest <span class="mdl">{cm}</span>, fastest '
                '<span class="mdl">{fm}</span> '
                '<span class="why">(the two lenses disagree here)</span></li>'.format(
                    cat=esc(lbl(c)), cm=esc(mc), fm=esc(ml_)))
        else:
            parts.append(
                '<li><span class="cat">{cat}</span> <span class="arrow">-></span> '
                'run <span class="mdl">{m}</span> '
                '<span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), m=esc(i["model"]), why=esc(i["reason"])))
    parts.append("</ul></div>")
    return "".join(parts)


def _dual_routing_html(cat_aggs, bar, disc_bar, labels):
    """Per-service dual routing table: for each category the CHEAPEST model that
    clears the bar (with its cost/test) and the FASTEST (with its median
    latency). When the two picks are the SAME model the cell collapses across
    both columns and notes "cheapest and fastest". When no model clears, the row
    says so and names the closest (strongest) model."""
    esc = html.escape
    dr = dual_routing(cat_aggs, bar, disc_bar)
    parts = ['<h2>Per-service routing</h2>',
             '<table class="routing"><thead><tr>'
             '<th class="l">service</th><th class="l">cheapest</th>'
             '<th class="l">fastest</th></tr></thead><tbody>']
    for c in sorted(dr):
        lbl = category_label(c, labels)
        cheap_m, cheap_cost, _cl = dr[c]["cheapest"]
        fast_m, _fc, fast_lat = dr[c]["fastest"]
        if cheap_m is None:
            strong = strongest_model(cat_aggs[c])
            parts.append(
                '<tr class="none"><td class="l"><b>{cat}</b></td>'
                '<td class="l" colspan="2">no model clears the bar yet '
                '({strong} is closest)</td></tr>'.format(
                    cat=esc(lbl), strong=esc(str(strong))))
        elif cheap_m == fast_m:
            parts.append(
                '<tr><td class="l"><b>{cat}</b></td>'
                '<td class="l" colspan="2"><span class="mdl">{m}</span> '
                '<span class="rt-note">cheapest and fastest</span> '
                '<span class="rt-metric">{cost}, {lat}</span></td></tr>'.format(
                    cat=esc(lbl), m=esc(cheap_m),
                    cost=esc(fmt_cost(cheap_cost)), lat=esc(fmt_latency(fast_lat))))
        else:
            parts.append(
                '<tr><td class="l"><b>{cat}</b></td>'
                '<td class="l"><span class="mdl">{cm}</span> '
                '<span class="rt-metric">{cost}</span></td>'
                '<td class="l"><span class="mdl">{fm}</span> '
                '<span class="rt-metric">{lat}</span></td></tr>'.format(
                    cat=esc(lbl), cm=esc(cheap_m), cost=esc(fmt_cost(cheap_cost)),
                    fm=esc(fast_m), lat=esc(fmt_latency(fast_lat))))
    parts.append('</tbody></table>')
    return "".join(parts)


def _drilldown_html(owner_route, cat_aggs, cat_fails, labels, incumbent):
    """Per-category <details>, closed by default. Summary = the owner line;
    expansion = the per-model table for that category plus the DEDUPED floor
    failures within it."""
    esc = html.escape
    parts = ['<h2>Per-category detail</h2>']
    for c in sorted(cat_aggs):
        i = owner_route.get(c, {"model": None, "reason": "", "strongest": None})
        lbl = category_label(c, labels)
        if i["model"] is None:
            summ = ('<span class="cat">{cat}</span> <span class="arrow">-></span> '
                    '<span class="mdl">NO MODEL CLEARS THE BAR</span>'
                    '<span class="why">{why}</span>').format(
                        cat=esc(lbl),
                        why=(" - " + esc(i["reason"])) if i.get("reason") else "")
            none_attr = ' data-none="1"'
        else:
            summ = ('<span class="cat">{cat}</span> <span class="arrow">-></span> '
                    'run <span class="mdl">{m}</span>'
                    '<span class="why">({why})</span>').format(
                        cat=esc(lbl), m=esc(i["model"]), why=esc(i["reason"]))
            none_attr = ''
        parts.append('<details class="cat"' + none_attr + '><summary>' + summ + '</summary>')
        parts.append('<div class="cat-body">')
        parts.append(_model_table(cat_aggs[c], i["model"], incumbent))
        fails = cat_fails.get(c, [])
        if fails:
            parts.append('<div class="fail-head">Floor tests failed here '
                         '(deduped across repeats)</div><ul class="fails">')
            for m, test, nf, runs in sorted(fails):
                parts.append('<li><b>{m}</b> fails {t} ({nf}/{runs} runs)</li>'.format(
                    m=esc(m), t=esc(test), nf=nf, runs=runs))
            parts.append("</ul>")
        else:
            parts.append('<p class="clean">Every model cleared every floor test '
                         'in this category.</p>')
        parts.append("</div></details>")
    return "".join(parts)


def _suite_health_html(tests, layered):
    """Deduped suite-health block: floor failures counted per (model, test), and
    saturated discriminating tests (one line per test)."""
    esc = html.escape
    parts = ['<h2>Suite health</h2>']
    if not layered:
        parts.append('<p class="note">Tests were not tagged by layer, so no '
                     'saturation / regression checks were run (they need '
                     'floor / discriminating tags).</p>')
        return "".join(parts)
    saturated, regressions = suite_health(tests)
    if not saturated and not regressions:
        parts.append('<p class="clean">No issues: no saturated discriminating '
                     'tests, and every model cleared every floor test.</p>')
        return "".join(parts)
    total_models = max((t["n_models"] for t in tests.values()
                        if t["layer"] == "floor"), default=0)
    if regressions:
        import collections
        by_model = collections.OrderedDict()
        fail_models_by_test = collections.defaultdict(set)
        for m, test, nf, runs in regressions:
            by_model.setdefault(m, []).append((test, nf, runs))
            fail_models_by_test[test].add(m)

        # Tests failed by many models are almost always the TEST's fault (too
        # strict, or naked-recall), not a real per-model regression. Surface them
        # first so the reviewer fixes the suite, not the models.
        thresh = max(2, (total_models + 1) // 2) if total_models else 2
        suspect = sorted(((t, len(ms)) for t, ms in fail_models_by_test.items()
                          if len(ms) >= thresh), key=lambda x: (-x[1], x[0]))
        if suspect:
            parts.append('<div class="fail-head" style="color:#8a6d00">Floor tests '
                         'failed by many models (likely the TEST, not the models - '
                         'too strict or naked-recall; review these first)</div>'
                         '<ul class="sat">')
            for t, n in suspect:
                parts.append('<li>{t} <span style="color:#889">- fails {n}/{tot} '
                             'models</span></li>'.format(
                                 t=esc(t), n=n, tot=(total_models or "?")))
            parts.append("</ul>")

        # Per-model, collapsible (worst first). Collapsed by default so the section
        # stays short; Expand-all opens them.
        parts.append('<div class="fail-head">Floor failures by model</div>')
        for m in sorted(by_model, key=lambda k: (-len(by_model[k]), k)):
            rows = sorted(by_model[m])
            parts.append(
                '<details class="cat"><summary><span class="cat">{m}</span> '
                '<span class="why">{n} floor test{s} failed</span></summary>'
                '<div class="cat-body"><ul class="fails">'.format(
                    m=esc(m), n=len(rows), s=("" if len(rows) == 1 else "s")))
            for test, nf, runs in rows:
                parts.append('<li>{t} <span style="color:#889">({nf}/{runs} runs)'
                             '</span></li>'.format(t=esc(test), nf=nf, runs=runs))
            parts.append('</ul></div></details>')
    if saturated:
        parts.append('<div class="fail-head" style="color:#8a6d00">Saturated '
                     'discriminating tests (no ranking signal; harden or retire)'
                     '</div><ul class="sat">')
        for test in saturated:
            parts.append('<li>' + esc(test) + "</li>")
        parts.append("</ul>")
    return "".join(parts)


def _run_cost_html(records, judge_id=None):
    """A run-cost-and-tokens panel: per-model generation tokens (prompt /
    completion / reasoning) and cost, a generation subtotal, the grading (judge)
    tokens with an estimated cost, and a grand total. Mirrors promptfoo's
    end-of-run token summary and adds the spend. Generation cost is promptfoo's own
    per-record figure; grading cost is estimated from the judge's grading tokens at
    its list price (see _PRICE_PER_M), since promptfoo does not price grading."""
    esc = html.escape
    import collections
    prov = collections.OrderedDict()
    g_p = g_c = 0
    for rec in records or []:
        pid = rec.get("provider")
        pid = pid.get("id") if isinstance(pid, dict) else pid
        pid = pid or "?"
        tu = (rec.get("response") or {}).get("tokenUsage") or rec.get("tokenUsage") or {}
        cd = tu.get("completionDetails") or {}
        d = prov.setdefault(pid, {"req": 0, "p": 0, "c": 0, "r": 0, "t": 0, "cost": 0.0})
        d["req"] += 1
        p_ = tu.get("prompt") or 0
        c_ = tu.get("completion") or 0
        d["p"] += p_
        d["c"] += c_
        d["r"] += cd.get("reasoning") or 0
        # Use promptfoo's own per-record total: providers disagree on whether
        # reasoning is counted inside completion (opus) or added on top (gemini),
        # and this total resolves it. Fall back to prompt+completion if absent.
        d["t"] += tu.get("total") or (p_ + c_)
        cst = rec.get("cost")
        if isinstance(cst, (int, float)):
            d["cost"] += cst
        gtu = (rec.get("gradingResult") or {}).get("tokensUsed") or {}
        g_p += gtu.get("prompt") or 0
        g_c += gtu.get("completion") or 0
    if not prov:
        return ""
    tot = {"req": 0, "p": 0, "c": 0, "r": 0, "t": 0, "cost": 0.0}
    for d in prov.values():
        for k in tot:
            tot[k] += d[k]
    gen_tokens = tot["t"]
    grade_tokens = g_p + g_c
    total_tokens = gen_tokens + grade_tokens
    jprice = None
    if judge_id:
        for k, v in _PRICE_PER_M.items():
            if k in judge_id:
                jprice = v
                break
    grade_cost = (g_p * jprice[0] / 1e6 + g_c * jprice[1] / 1e6) if jprice else None
    total_cost = tot["cost"] + (grade_cost or 0.0)
    jname = esc(judge_id.split(":")[-1]) if judge_id else "?"
    C = lambda n: "{:,}".format(int(n))
    D = lambda x: "${:,.2f}".format(x)

    out = ['<h2>Run cost and tokens</h2>']
    if grade_cost is not None:
        head = ('Total run spend <b>~{tc}</b>: generation {gc} + grading ~{grc} '
                '(judge {j}, estimated). {tt} tokens = {gen} generation + {grd} '
                'grading.').format(tc=D(total_cost), gc=D(tot["cost"]),
                                   grc=D(grade_cost), j=jname, tt=C(total_tokens),
                                   gen=C(gen_tokens), grd=C(grade_tokens))
    else:
        head = ('Generation spend <b>{gc}</b> over {gen} generation tokens; grading '
                'used {grd} tokens on the judge (not auto-priced here). {tt} tokens '
                'total.').format(gc=D(tot["cost"]), gen=C(gen_tokens),
                                 grd=C(grade_tokens), tt=C(total_tokens))
    out.append('<p class="runcost-head">' + head + '</p>')
    out.append('<table class="runcost"><thead><tr><th class="l">model</th>'
               '<th>requests</th><th>prompt</th><th>completion</th><th>reasoning</th>'
               '<th>total tokens</th><th>cost</th></tr></thead><tbody>')
    for pid, d in sorted(prov.items(),
                         key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"]))):
        out.append('<tr><td class="l">{m}</td><td>{rq}</td><td>{p}</td><td>{c}</td>'
                   '<td>{r}</td><td>{t}</td><td>{cost}</td></tr>'.format(
                       m=esc(pid), rq=C(d["req"]), p=C(d["p"]), c=C(d["c"]),
                       r=C(d["r"]), t=C(d["t"]),
                       cost=(D(d["cost"]) if d["cost"] else "$0.00 (local)")))
    out.append('<tr class="subtotal"><td class="l">generation subtotal</td>'
               '<td>{rq}</td><td>{p}</td><td>{c}</td><td>{r}</td><td>{t}</td>'
               '<td>{cost}</td></tr>'.format(rq=C(tot["req"]), p=C(tot["p"]),
                                             c=C(tot["c"]), r=C(tot["r"]),
                                             t=C(gen_tokens), cost=D(tot["cost"])))
    grc = ("~" + D(grade_cost) + " est" if grade_cost is not None else "not auto-priced")
    out.append('<tr class="grading"><td class="l">grading (judge {j})</td><td></td>'
               '<td>{p}</td><td>{c}</td><td>-</td><td>{t}</td><td>{gc}</td></tr>'.format(
                   j=jname, p=C(g_p), c=C(g_c), t=C(grade_tokens), gc=grc))
    out.append('<tr class="total"><td class="l">total</td><td></td><td></td><td></td>'
               '<td></td><td>{t}</td><td>{tc}</td></tr>'.format(
                   t=C(total_tokens),
                   tc=("~" + D(total_cost) if grade_cost is not None else D(tot["cost"]))))
    out.append('</tbody></table>')
    out.append('<p class="runcost-note">Generation cost is promptfoo\'s own '
               'per-model figure. Grading cost is a LIST-PRICE UPPER BOUND: the '
               'judge\'s grading tokens at its list rate, with no caching or volume '
               'discounts, and promptfoo does not meter grading itself. Actual billed '
               'cost is usually lower, and provider usage dashboards lag (Anthropic '
               'more than most), so confirm real spend in each provider\'s usage '
               'console. Local models are free.</p>')
    return "".join(out)


def render_html(agg, layered, bar, disc_bar, rec_model_id, incumbent, tests=None,
                cat_aggs=None, categorized=False, optimize="cost", records=None,
                labels=None, judge_id=None, top_n=8):
    esc = html.escape
    labels = labels or {}
    cat_aggs = cat_aggs if cat_aggs is not None else {}
    # Both routing lenses are always computed. The headline leads with --optimize;
    # the frontier charts and the per-service table always show both.
    route_cost = owner_routing(cat_aggs, bar, disc_bar, "cost")
    route_lat = owner_routing(cat_aggs, bar, disc_bar, "latency")
    lead_route = route_lat if optimize == "latency" else route_cost
    everyday = everyday_pick(lead_route, optimize, agg)
    note = ("" if layered else
            '<p class="note">Tests were not tagged by layer, so floor = overall '
            'pass-rate and disc = mean score across all tests.</p>')

    if categorized and lead_route:
        owner = _owner_summary_html(lead_route, route_cost, route_lat, everyday,
                                    labels, optimize)
    else:
        pick = ("fastest by median latency" if optimize == "latency" else "cheapest")
        line = ("Run <b>{}</b>, the {} model that clears the bar.".format(
            esc(rec_model_id), pick) if rec_model_id
            else "No model clears the bar (floor pass-rate at or above "
                 "{:.0f}%).".format(bar * 100))
        owner = ('<div class="owner"><p class="headline">' + line + "</p>"
                 '<p class="why" style="color:#778">No test carried a category, so '
                 'per-category routing is absent. Tag tests with metadata.category '
                 'to enable it.</p></div>')

    # Two frontier charts, always: overall cheapest and overall fastest clearer
    # are ringed in their respective charts; both share the model color map and
    # the same zoomed y-axis floor. One shared legend serves both.
    colors = _model_colors(agg)
    ylo = _frontier_ylo(agg)
    rec_cost = recommend(agg, bar, disc_bar, "cost")
    rec_lat = recommend(agg, bar, disc_bar, "latency")
    legend = _frontier_legend_html(agg, colors, rec_cost, rec_lat, incumbent, top_n)
    svg_cost = _svg_frontier_plot(agg, colors, "cost", rec_cost, incumbent, ylo, top_n)
    svg_lat = _svg_frontier_plot(agg, colors, "latency", rec_lat, incumbent, ylo, top_n)
    frontiers = (legend
                 + '<div class="frontier-row">'
                 + '<div class="chart"><div class="chart-title">cost vs quality'
                   '</div>' + svg_cost + '</div>'
                 + '<div class="chart"><div class="chart-title">latency vs quality'
                   '</div>' + svg_lat + '</div>'
                 + '</div>')

    overall_table = _model_table(agg, rec_cost, incumbent)
    if categorized and cat_aggs:
        dual_table = _dual_routing_html(cat_aggs, bar, disc_bar, labels)
        cat_fails = category_floor_failures(records or [])
        drilldown = _drilldown_html(lead_route, cat_aggs, cat_fails, labels, incumbent)
    else:
        dual_table = ""
        drilldown = ""
    health = _suite_health_html(tests if tests is not None else {}, layered)
    runcost = _run_cost_html(records or [], judge_id)
    db = ("" if disc_bar <= 0
          else ", disc score at or above {:.2f}".format(disc_bar))

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clawhound model recommendation</title>
<style>{css}</style></head><body>
<div class="controls">
<button onclick="cwAll(true)">Expand all</button>
<button onclick="cwAll(false)">Collapse all</button>
</div>
<div class="wrap">
<h1>Which model to run</h1>
{owner}
{note}
<h2>Cost and latency vs quality</h2>
{frontiers}
<h2>All models at a glance</h2>
<p style="font-size:.83rem;color:#4a5568;background:#f3f5f8;border:1px solid #e2e6ec;border-radius:6px;padding:.55rem .75rem;margin:.4rem 0 .9rem;line-height:1.55"><b>floor</b>: must-pass correctness and guardrails, the routing gate (you want 100%). &nbsp; <b>disc</b>: discriminating, the graded hard-reasoning score, 0 to 1, used to rank models. &nbsp; <b>latency</b>: median response time. &nbsp; <b>cost /100</b>: average API cost per 100 tests (per-test is fractions of a cent; the run-cost panel below shows real totals).</p>
{overall_table}
{dual_table}
{drilldown}
{runcost}
{health}
<p class="foot">Bar: floor pass-rate at or above {barp:.0f}%{db}. Generated by clawhound from a promptfoo results file.</p>
</div>
<script>{script}</script>
</body></html>""".format(
        css=_REPORT_CSS, owner=owner, note=note, frontiers=frontiers,
        dual_table=dual_table, overall_table=overall_table,
        drilldown=drilldown, health=health, barp=bar * 100, db=db,
        runcost=runcost, script=_REPORT_JS)


# ----------------------------------------------------------------------------
# Built-in sample-results test. No file, no network: hand-built promptfoo-shaped
# records that exercise the suite-health block. Run with `--selftest`.
# ----------------------------------------------------------------------------

def _sample_records():
    # latency_ms lets the same fixtures exercise --optimize latency. The key
    # divergence: opus is PRICIER but FASTER, haiku is CHEAPER but SLOWER. So in
    # a category both clear, cost mode picks haiku (cheap) and latency mode picks
    # opus (fast) -- the two metrics select different models.
    def rec(model, test, layer, success, score, cost, category, latency_ms):
        return {
            "provider": {"id": model},
            "testCase": {"description": test,
                         "metadata": {"layer": layer, "category": category}},
            "success": success,
            "score": score,
            "cost": cost,
            "latencyMs": latency_ms,
        }

    # opus: expensive (0.02) but fast (500 ms). haiku: cheap (0.002) but slow (3000 ms).
    recs = []
    # --- category "math": a floor test only the EXPENSIVE model clears --------
    # A floor test EVERY model passes: this is the alarm working, must NOT flag.
    recs.append(rec("anthropic:opus", "odds axiom deflate/inflate", "floor", True, 1.0, 0.02, "math", 500))
    recs.append(rec("anthropic:haiku", "odds axiom deflate/inflate", "floor", True, 1.0, 0.002, "math", 3000))
    # A floor test one model FAILS: regression / coverage gap flag. Because haiku
    # fails a math floor test, only opus clears the bar in "math" -> math routes opus.
    recs.append(rec("anthropic:opus", "never fabricate a score", "floor", True, 1.0, 0.02, "math", 500))
    recs.append(rec("anthropic:haiku", "never fabricate a score", "floor", False, 0.0, 0.002, "math", 3000))
    # --- category "frontend": both clear the floor. This is the divergence case:
    # cost mode -> haiku (cheaper), latency mode -> opus (faster). ------------
    recs.append(rec("anthropic:opus", "tailwind slate palette copy", "floor", True, 1.0, 0.02, "frontend", 500))
    recs.append(rec("anthropic:haiku", "tailwind slate palette copy", "floor", True, 1.0, 0.002, "frontend", 3000))
    # --- category "theory": discriminating-only, so nobody has a floor bar ----
    # A discriminating test EVERY model passes (score >= 0.5): saturated flag.
    recs.append(rec("anthropic:opus", "diagnose devig longshot bias", "discriminating", True, 0.85, 0.02, "theory", 500))
    recs.append(rec("anthropic:haiku", "diagnose devig longshot bias", "discriminating", True, 0.72, 0.002, "theory", 3000))
    # A discriminating test that still SPLITS the models: healthy, must NOT flag.
    recs.append(rec("anthropic:opus", "raw CLV does not prove edge", "discriminating", True, 0.78, 0.02, "theory", 500))
    recs.append(rec("anthropic:haiku", "raw CLV does not prove edge", "discriminating", False, 0.30, 0.002, "theory", 3000))
    return recs


def _selftest():
    records = _sample_records()
    agg, layered = aggregate(records)
    tests = aggregate_tests(records)
    cat_aggs, categorized = aggregate_by_category(records)
    rec = recommend(agg, 1.0, 0.0)
    print_report(agg, layered, 1.0, 0.0, rec, "anthropic:opus")
    print_routing(cat_aggs, categorized, 1.0, 0.0)
    print_health(tests, layered)

    saturated, regressions = suite_health(tests)
    assert "diagnose devig longshot bias" in saturated, saturated
    assert "raw CLV does not prove edge" not in saturated, saturated
    # a floor test all models pass is the alarm working, never "saturated"
    assert "odds axiom deflate/inflate" not in saturated, saturated
    # regressions are DEDUPED per (model, test) with (fails, runs) counts.
    assert any(m == "anthropic:haiku" and t == "never fabricate a score"
               for m, t, _f, _r in regressions), regressions
    # the all-pass floor test must not appear as a regression
    assert all(t != "odds axiom deflate/inflate" for _m, t, _f, _r in regressions), regressions

    # Per-category routing: different models win in different categories.
    assert categorized, "sample records should be categorized"
    routing = route_by_category(cat_aggs, 1.0, 0.0)
    # math: haiku fails a floor test there, so only the expensive opus clears.
    assert routing["math"][0] == "anthropic:opus", routing
    # frontend: both clear the floor, so the cheapest model (haiku) is chosen.
    assert routing["frontend"][0] == "anthropic:haiku", routing
    # frontend cost must be haiku's cheaper per-test cost, not opus's.
    assert routing["frontend"][1] == 0.002, routing
    # theory: discriminating-only, no floor bar to clear, so nobody is routed.
    assert routing["theory"][0] is None, routing

    # --- latency mode: the gate is the SAME (floor pass-rate) but the tiebreak
    # is median latency, so among models that clear it the FASTEST wins. -------
    # Median latency must be captured per model and per category (in seconds).
    assert agg["anthropic:opus"]["latency_s"] == 0.5, agg["anthropic:opus"]
    assert agg["anthropic:haiku"]["latency_s"] == 3.0, agg["anthropic:haiku"]
    routing_lat = route_by_category(cat_aggs, 1.0, 0.0, "latency")
    # frontend: both clear the floor. Cost mode picked the CHEAP haiku above;
    # latency mode must pick the FAST opus instead. Same gate, different metric.
    assert routing["frontend"][0] == "anthropic:haiku", routing
    assert routing_lat["frontend"][0] == "anthropic:opus", routing_lat
    # the returned latency column must be opus's median latency in seconds.
    assert routing_lat["frontend"][2] == 0.5, routing_lat
    # math: only opus clears the floor, so the metric cannot change the pick.
    assert routing_lat["math"][0] == "anthropic:opus", routing_lat
    # theory: nobody clears a floor bar, so latency mode routes nobody either.
    assert routing_lat["theory"][0] is None, routing_lat
    # overall recommendation must also respect the metric among floor-clearers.
    rec_lat = recommend(agg, 1.0, 0.0, "latency")
    assert rec_lat == "anthropic:opus", rec_lat  # only opus clears overall floor

    # --- dedupe across repeats: a floor test failing on every one of K runs must
    # appear ONCE as (model, test, K, K), never K separate lines. ------------
    repeated = _sample_records() + _sample_records() + _sample_records()
    _sat_r, regs_r = suite_health(aggregate_tests(repeated))
    fab = [t for t in regs_r if t[1] == "never fabricate a score"]
    assert len(fab) == 1, fab  # deduped to one row
    assert fab[0] == ("anthropic:haiku", "never fabricate a score", 3, 3), fab

    # --- owner summary: the everyday pick is the model recommended for the MOST
    # categories, derived from the routing (never hardcoded). ----------------
    owner_route = owner_routing(cat_aggs, 1.0, 0.0)
    assert owner_route["math"]["model"] == "anthropic:opus", owner_route["math"]
    assert owner_route["math"]["reason"] == "the only model that clears the bar", owner_route["math"]
    assert owner_route["frontend"]["model"] == "anthropic:haiku", owner_route["frontend"]
    assert owner_route["frontend"]["reason"] == "cheapest that clears the bar", owner_route["frontend"]
    assert owner_route["theory"]["model"] is None, owner_route["theory"]
    assert owner_route["theory"]["strongest"] == "anthropic:opus", owner_route["theory"]
    # cost mode: math->opus, frontend->haiku (1 each); tie broken by cheaper
    # overall -> haiku is the everyday pick.
    everyday = everyday_pick(owner_route, "cost", agg)
    assert everyday == "anthropic:haiku", everyday
    # the zoomed y-axis lower bound never starts above 80.
    assert _frontier_ylo(agg) <= 80, _frontier_ylo(agg)

    # --- dual routing: cheapest and fastest computed once per metric. The set
    # of clearers is metric-independent, so where more than one model clears the
    # two lenses can name DIFFERENT models. Frontend is the divergence case. ---
    dr = dual_routing(cat_aggs, 1.0, 0.0)
    assert dr["frontend"]["cheapest"][0] == "anthropic:haiku", dr["frontend"]
    assert dr["frontend"]["fastest"][0] == "anthropic:opus", dr["frontend"]
    # cheapest column carries haiku's per-test cost; fastest carries opus's latency.
    assert dr["frontend"]["cheapest"][1] == 0.002, dr["frontend"]
    assert dr["frontend"]["fastest"][2] == 0.5, dr["frontend"]
    # math: only opus clears, so cheapest and fastest agree (collapse case).
    assert dr["math"]["cheapest"][0] == "anthropic:opus", dr["math"]
    assert dr["math"]["fastest"][0] == "anthropic:opus", dr["math"]
    # theory: nobody clears a floor bar, so neither lens routes a model.
    assert dr["theory"]["cheapest"][0] is None, dr["theory"]
    assert dr["theory"]["fastest"][0] is None, dr["theory"]

    # HTML report must carry the suite-health block, the dual routing table, and
    # BOTH frontier charts under one shared legend.
    doc = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                      cat_aggs, categorized, records=records)
    assert "Suite health" in doc, "suite-health block missing from HTML"
    assert "diagnose devig longshot bias" in doc, "saturated test missing from HTML"
    assert "Per-service routing" in doc, "dual routing table missing from HTML"
    assert "Per-category detail" in doc, "per-category drilldown missing from HTML"
    # two frontier charts: one cost-x, one latency-x, both axis titles present.
    assert "cost vs quality" in doc, "cost frontier title missing from HTML"
    assert "latency vs quality" in doc, "latency frontier title missing from HTML"
    assert "cost per 100 tests (USD)" in doc, "cost x-axis label missing from HTML"
    assert "median latency per test (s)" in doc, "latency x-axis label missing from HTML"
    # exactly ONE shared model legend serves both charts.
    assert doc.count('class="frontier-legend"') == 1, "legend must be shared, not per-chart"
    # dual routing table: a cheapest and a fastest column header.
    assert ">cheapest<" in doc and ">fastest<" in doc, "dual routing columns missing"
    # frontend diverges: the disagreement note must fire and name both picks.
    assert "Cheapest and fastest picks differ" in doc, "disagreement note missing"
    assert "the two lenses disagree here" in doc, "per-category disagreement line missing"
    # math collapses to one model that is both cheapest and fastest.
    assert "cheapest and fastest" in doc, "collapsed same-model routing cell missing"
    # KEEP: legend paragraph, all-models table, incumbent marker, collapse-all.
    assert "you are here" in doc, "incumbent marker missing from HTML"
    assert "NO MODEL CLEARS THE BAR" in doc, "no-clear marking missing from HTML"
    assert "for everyday work" in doc, "owner headline missing from HTML"
    assert "Expand all" in doc and "Collapse all" in doc, "collapse-all control missing"
    assert "<details class=\"cat\"" in doc, "collapsible category detail missing"
    assert "Headline follows your <b>cheapest</b> lens" in doc, "cost-lens headline note missing"

    # --optimize only flips which lens the HEADLINE leads with; both frontiers
    # and both routing picks always show, so the two docs share the same charts.
    doc_lat = render_html(agg, layered, 1.0, 0.0, rec_lat, "anthropic:opus", tests,
                          cat_aggs, categorized, optimize="latency", records=records)
    assert "cost vs quality" in doc_lat and "latency vs quality" in doc_lat, \
        "both frontiers must render regardless of --optimize"
    assert "Per-service routing" in doc_lat, "dual routing must render in latency mode"
    assert "Headline follows your <b>fastest</b> lens" in doc_lat, "latency-lens headline note missing"
    print("\nselftest: OK")


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
    ap.add_argument("--top-n", type=int, default=8,
                    help="show the top N models (by floor) as individual dots on the "
                         "frontier charts; collapse the rest into one gray '+X more' "
                         "point at the best remainder's coordinates (default 8; 0 "
                         "disables grouping)")
    ap.add_argument("--out", default="report.html", help="HTML report path")
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
    rec = recommend(agg, args.bar, args.disc_bar, args.optimize)
    print_report(agg, layered, args.bar, args.disc_bar, rec, args.incumbent, args.optimize)
    print_routing(cat_aggs, categorized, args.bar, args.disc_bar, args.optimize)
    print_health(tests, layered)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(agg, layered, args.bar, args.disc_bar, rec,
                            args.incumbent, tests, cat_aggs, categorized, args.optimize,
                            records=records, labels=labels, judge_id=judge_id,
                            top_n=args.top_n))
    print("\nHTML report: " + args.out)


if __name__ == "__main__":
    main()
