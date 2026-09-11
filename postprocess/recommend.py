#!/usr/bin/env python
"""clawhound recommend: turn a promptfoo results file into a decision.

promptfoo runs the evals across every provider and shows you the numbers. It
does not DECIDE. This reads `promptfoo eval --output results.json` and answers
the question a buyer actually has: which model should I run, does it clear my
bar, and what does it cost. It prints a ranked table and a recommendation, and
writes a self-contained HTML report whose centerpiece is a cost-vs-quality
frontier (inline SVG, no external libraries, opens in any browser).

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

--optimize picks the tiebreak AMONG models that clear the bar: cost (default,
cheapest per test) or latency (fastest by median per-call latency). Latency mode
suits a flat-rate / subscription user who pays no per-token cost and just wants
the quickest model that reliably clears the bar. The bar itself is unchanged.
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
    if x is None:
        return "n/a"
    if x == 0:
        return "$0"
    # cost per test is usually small; show per test and per 1k tests
    return "${:.5f}".format(x).rstrip("0").rstrip(".")


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
        "model", "floor", "disc", "cost/test", "latency", w=name_w)
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
        "category", "model", "cost/test", "latency", cw=cat_w, mw=mdl_w)
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


def _svg_frontier(agg, rec_model_id, incumbent, optimize="cost"):
    latency = (optimize == "latency")
    field = "latency_s" if latency else "cost_per_test"
    axis_word = "median latency" if latency else "cost"
    pts = [(m, s) for m, s in agg.items()
           if s.get(field) is not None and s.get("floor_rate") is not None]
    W, H = 760, 380
    ml, mr, mt, mb = 62, 210, 24, 56
    pw, ph = W - ml - mr, H - mt - mb
    if not pts:
        return ('<svg width="{w}" height="80"><text x="10" y="45" '
                'font-family="system-ui" font-size="14">No models reported a {a}; '
                'nothing to plot on the {a} axis.</text></svg>').format(w=W, a=axis_word)
    max_x = max(s[field] for _, s in pts) or 1e-9
    max_x *= 1.15
    ylo = _frontier_ylo(agg)          # percent
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
    # y grid + labels (zoomed pass-rate ylo..100)
    for i in range(0, 5):
        pct = ylo + (100 - ylo) * i / 4.0
        y = py(pct / 100.0)
        parts.append('<line x1="{x}" y1="{y}" x2="{r}" y2="{y}" stroke="#eef0f4"/>'.format(
            x=ml, y=y, r=ml + pw))
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#667" '
                     'text-anchor="end">{v:.0f}%</text>'.format(x=ml - 8, y=y + 4, v=pct))
    # x labels (active metric: cost per test, or median latency in seconds)
    for i in range(0, 5):
        c = max_x * i / 4.0
        x = px(c)
        lbl = "{v:.3f}s".format(v=c) if latency else "${v:.4f}".format(v=c)
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#667" '
                     'text-anchor="middle">{l}</text>'.format(x=x, y=mt + ph + 18, l=lbl))
    axis_title = ("median latency per test (seconds), lower is better" if latency
                  else "cost per test (USD), lower is better")
    parts.append('<text x="{x}" y="{y}" font-size="12" fill="#333" '
                 'text-anchor="middle">{t}</text>'.format(
                     x=ml + pw / 2, y=H - 10, t=html.escape(axis_title)))
    parts.append('<text transform="translate(15,{y}) rotate(-90)" font-size="12" '
                 'fill="#333" text-anchor="middle">floor pass-rate</text>'.format(
                     y=mt + ph / 2))
    # dots only (no inline labels -> a compact legend carries the names, so
    # nearby dots never collide). Recommended ringed, incumbent in purple.
    for m, s in pts:
        x, y = px(s[field]), py(s["floor_rate"])
        recommended = (m == rec_model_id)
        is_inc = (incumbent and m == incumbent)
        color = "#1a7f37" if recommended else ("#8250df" if is_inc else "#0969da")
        if recommended:
            parts.append('<circle cx="{x}" cy="{y}" r="11" fill="none" '
                         'stroke="#1a7f37" stroke-width="2"/>'.format(x=x, y=y))
        parts.append('<circle cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(x=x, y=y, c=color))
    # compact legend: colored dot + full model id + its metric and pass-rate.
    # Vertical list in the right margin, so labels stack and never overlap.
    lx = ml + pw + 22
    ly = mt + 10
    legend_rows = sorted(pts, key=optimize_sort_key(optimize))
    for m, s in legend_rows:
        recommended = (m == rec_model_id)
        is_inc = (incumbent and m == incumbent)
        color = "#1a7f37" if recommended else ("#8250df" if is_inc else "#0969da")
        metric_txt = (fmt_latency(s["latency_s"]) if latency
                      else fmt_cost(s["cost_per_test"]))
        tag = " *rec" if recommended else (" *here" if is_inc else "")
        parts.append('<circle cx="{x}" cy="{y}" r="5" fill="{c}"/>'.format(
            x=lx, y=ly, c=color))
        parts.append('<text x="{tx}" y="{ty}" font-size="11" fill="#222">{l}</text>'.format(
            tx=lx + 11, ty=ly + 4, l=html.escape(m + tag)))
        parts.append('<text x="{tx}" y="{ty}" font-size="10" fill="#889">{l}</text>'.format(
            tx=lx + 11, ty=ly + 17,
            l=html.escape(fmt_rate(s["floor_rate"]) + " floor, " + metric_txt)))
        ly += 40
    # legend key for the markers
    parts.append('<text x="{tx}" y="{ty}" font-size="10" fill="#99a">'
                 '*rec = recommended{inc}</text>'.format(
                     tx=lx, ty=ly + 4,
                     inc=", *here = you are here" if incumbent else ""))
    parts.append("</svg>")
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
.chart svg { display: block; max-width: 100%; height: auto; }
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
"""


def _model_table(agg, winner, incumbent):
    """Compact per-model table: floor pass-rate, discriminating score, latency,
    cost, with the winner (recommended for this scope) and incumbent marked."""
    esc = html.escape
    rows = sorted(agg.items(), key=lambda kv: (kv[1]["floor_rate"] is None,
                                               -(kv[1]["floor_rate"] or 0)))
    out = ['<table class="models"><thead><tr>'
           '<th class="l">model</th><th>floor</th><th>disc</th>'
           '<th>latency</th><th>cost/test</th><th class="l"></th>'
           '</tr></thead><tbody>']
    for m, s in rows:
        is_win = (m == winner)
        is_here = (incumbent and m == incumbent)
        tag = ('<span class="win-tag">run this</span>' if is_win
               else ('<span class="here-tag">you are here</span>' if is_here else ""))
        out.append(
            '<tr class="{cls}"><td class="l">{m}</td><td>{fr}</td><td>{d}</td>'
            '<td>{lat}</td><td>{c}</td><td class="l">{tag}</td></tr>'.format(
                cls="win" if is_win else "",
                m=esc(m), fr=esc(fmt_rate(s["floor_rate"])),
                d=esc(fmt_score(s["disc"])), lat=esc(fmt_latency(s["latency_s"])),
                c=esc(fmt_cost(s["cost_per_test"])), tag=tag))
    out.append("</tbody></table>")
    return "".join(out)


def _owner_summary_html(owner_route, everyday, labels, bar, disc_bar, optimize):
    """Always-visible, plain-language routing summary an owner can read.

    Headline: the everyday pick (recommended for the most categories) and the
    exceptions (categories that route elsewhere or that nothing clears). No model
    name is hardcoded, so it reads naturally with any provider field.
    """
    esc = html.escape

    def lbl(c):
        return category_label(c, labels)

    parts = ['<div class="owner">']
    if everyday is None:
        parts.append('<p class="headline">No model clears the bar on any category '
                     'yet. Review the checks in each section below.</p>')
    else:
        everyday_cats = sorted(lbl(c) for c, i in owner_route.items()
                               if i["model"] == everyday)
        other = {}
        none_cats = []
        for c, i in owner_route.items():
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

    parts.append("<ul>")
    for c in sorted(owner_route):
        i = owner_route[c]
        if i["model"] is None:
            parts.append(
                '<li class="none"><span class="cat">{cat}</span> '
                '<span class="arrow">-></span> <span class="mdl">no model clears '
                'it yet</span> <span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), why=esc(i["reason"])))
        else:
            parts.append(
                '<li><span class="cat">{cat}</span> <span class="arrow">-></span> '
                'run <span class="mdl">{m}</span> '
                '<span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), m=esc(i["model"]), why=esc(i["reason"])))
    parts.append("</ul></div>")
    return "".join(parts)


def _drilldown_html(owner_route, cat_aggs, cat_fails, labels, incumbent):
    """Per-category <details>, closed by default. Summary = the owner line;
    expansion = the per-model table for that category plus the DEDUPED floor
    failures within it."""
    esc = html.escape
    parts = ['<h2>Per-category routing</h2>']
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
    if regressions:
        parts.append('<div class="fail-head">Floor failures (regression or '
                     'coverage gap)</div><ul class="fails">')
        for m, test, nf, runs in regressions:
            parts.append('<li><b>{m}</b> fails {t} ({nf}/{runs} runs)</li>'.format(
                m=esc(m), t=esc(test), nf=nf, runs=runs))
        parts.append("</ul>")
    if saturated:
        parts.append('<div class="fail-head" style="color:#8a6d00">Saturated '
                     'discriminating tests (no ranking signal; harden or retire)'
                     '</div><ul class="sat">')
        for test in saturated:
            parts.append('<li>' + esc(test) + "</li>")
        parts.append("</ul>")
    return "".join(parts)


def render_html(agg, layered, bar, disc_bar, rec_model_id, incumbent, tests=None,
                cat_aggs=None, categorized=False, optimize="cost", records=None,
                labels=None):
    esc = html.escape
    labels = labels or {}
    cat_aggs = cat_aggs if cat_aggs is not None else {}
    owner_route = owner_routing(cat_aggs, bar, disc_bar, optimize)
    everyday = everyday_pick(owner_route, optimize, agg)
    note = ("" if layered else
            '<p class="note">Tests were not tagged by layer, so floor = overall '
            'pass-rate and disc = mean score across all tests.</p>')

    if categorized and owner_route:
        owner = _owner_summary_html(owner_route, everyday, labels, bar, disc_bar, optimize)
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

    frontier = _svg_frontier(agg, rec_model_id, incumbent, optimize)
    overall_table = _model_table(agg, rec_model_id, incumbent)
    if categorized and cat_aggs:
        cat_fails = category_floor_failures(records or [])
        drilldown = _drilldown_html(owner_route, cat_aggs, cat_fails, labels, incumbent)
    else:
        drilldown = ""
    health = _suite_health_html(tests if tests is not None else {}, layered)
    frontier_title = ("Latency vs quality" if optimize == "latency"
                      else "Cost vs quality")
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
<h2>{frontier_title}</h2>
<div class="chart">{frontier}</div>
<h2>All models at a glance</h2>
{overall_table}
{drilldown}
{health}
<p class="foot">Bar: floor pass-rate at or above {barp:.0f}%{db}. Generated by clawhound from a promptfoo results file.</p>
</div>
<script>function cwAll(o){{document.querySelectorAll('details').forEach(function(d){{d.open=o;}});}}</script>
</body></html>""".format(
        css=_REPORT_CSS, owner=owner, note=note, frontier=frontier,
        frontier_title=frontier_title, overall_table=overall_table,
        drilldown=drilldown, health=health, barp=bar * 100, db=db)


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

    # HTML report must carry both the suite-health and the routing block.
    doc = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                      cat_aggs, categorized, records=records)
    assert "Suite health" in doc, "suite-health block missing from HTML"
    assert "diagnose devig longshot bias" in doc, "saturated test missing from HTML"
    assert "Per-category routing" in doc, "routing block missing from HTML"
    assert "NO MODEL CLEARS THE BAR" in doc, "no-clear marking missing from HTML"
    assert "Cost vs quality" in doc, "cost-mode frontier title missing from HTML"
    assert "for everyday work" in doc, "owner headline missing from HTML"
    assert "Expand all" in doc and "Collapse all" in doc, "collapse-all control missing"
    assert "<details class=\"cat\"" in doc, "collapsible category detail missing"
    # latency-mode HTML must relabel the frontier and show the latency metric.
    doc_lat = render_html(agg, layered, 1.0, 0.0, rec_lat, "anthropic:opus", tests,
                          cat_aggs, categorized, optimize="latency", records=records)
    assert "Latency vs quality" in doc_lat, "latency-mode frontier title missing"
    assert "median latency" in doc_lat, "latency axis label missing from HTML"
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
                    help="among models that clear the bar, pick the CHEAPEST "
                         "(cost, default) or the FASTEST by median latency "
                         "(latency). Latency suits a flat-rate user who pays no "
                         "per-token cost and just wants the quickest model that "
                         "reliably clears the bar. The bar itself is unchanged.")
    ap.add_argument("--incumbent", default=None,
                    help="provider:model you run today, marked 'you are here'")
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
                            records=records, labels=labels))
    print("\nHTML report: " + args.out)


if __name__ == "__main__":
    main()
