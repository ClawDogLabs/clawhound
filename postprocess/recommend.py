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
            "failed_models": [],
        })
        if t["layer"] is None and layer is not None:
            t["layer"] = layer
        t["n_models"] += 1
        model = rec_model(r)
        score = r.get("score")
        if layer == "discriminating":
            passed = isinstance(score, (int, float)) and float(score) >= 0.5
        else:
            # floor, or untagged: the deterministic must-pass boolean
            passed = bool(r.get("success"))
        if passed:
            t["n_passed"] += 1
        else:
            t["failed_models"].append(model)
    return tests


def suite_health(tests):
    """Return (saturated, regressions) from the per-test aggregate.

    saturated   list of discriminating test keys that EVERY model passed. These
                no longer rank anything, so they should be hardened or retired.
    regressions list of (model, test key) where a FLOOR test failed for a model.
                A floor test all models pass is NOT a problem (that is the alarm
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
            for m in t["failed_models"]:
                regressions.append((m, key))
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
    for m, test in regressions:
        print('FLOOR FAIL: model {} failed "{}": regression or coverage gap.'
              .format(m, test))
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

def _svg_frontier(agg, rec_model_id, incumbent, optimize="cost"):
    latency = (optimize == "latency")
    field = "latency_s" if latency else "cost_per_test"
    axis_word = "median latency" if latency else "cost"
    pts = [(m, s) for m, s in agg.items() if s.get(field) is not None]
    W, H = 720, 420
    ml, mr, mt, mb = 70, 30, 30, 60
    pw, ph = W - ml - mr, H - mt - mb
    if not pts:
        return ('<svg width="{w}" height="80"><text x="10" y="45" '
                'font-family="system-ui" font-size="14">No models reported a {a}; '
                'nothing to plot on the {a} axis.</text></svg>').format(w=W, a=axis_word)
    max_cost = max(s[field] for _, s in pts) or 1e-9
    max_cost *= 1.15

    def px(c):
        return ml + (c / max_cost) * pw

    def py(rate):
        r = 0.0 if rate is None else rate
        return mt + (1 - r) * ph

    parts = ['<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
             'font-family="system-ui, sans-serif">'.format(w=W, h=H)]
    parts.append('<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>'.format(w=W, h=H))
    # axes
    parts.append('<line x1="{x}" y1="{t}" x2="{x}" y2="{b}" stroke="#888"/>'.format(
        x=ml, t=mt, b=mt + ph))
    parts.append('<line x1="{x}" y1="{b}" x2="{r}" y2="{b}" stroke="#888"/>'.format(
        x=ml, b=mt + ph, r=ml + pw))
    # y grid + labels (pass-rate 0..100)
    for i in range(0, 5):
        r = i / 4.0
        y = py(r)
        parts.append('<line x1="{x}" y1="{y}" x2="{r}" y2="{y}" stroke="#eee"/>'.format(
            x=ml, y=y, r=ml + pw))
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#555" '
                     'text-anchor="end">{v:.0f}%</text>'.format(x=ml - 8, y=y + 4, v=r * 100))
    # x labels (active metric: cost per test, or median latency in seconds)
    for i in range(0, 5):
        c = max_cost * i / 4.0
        x = px(c)
        lbl = "{v:.3f}s".format(v=c) if latency else "${v:.4f}".format(v=c)
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#555" '
                     'text-anchor="middle">{l}</text>'.format(x=x, y=mt + ph + 18, l=lbl))
    axis_title = ("median latency per test (seconds)" if latency
                  else "cost per test (USD)")
    parts.append('<text x="{x}" y="{y}" font-size="12" fill="#333" '
                 'text-anchor="middle">{t}</text>'.format(
                     x=ml + pw / 2, y=H - 12, t=axis_title))
    parts.append('<text transform="translate(16,{y}) rotate(-90)" font-size="12" '
                 'fill="#333" text-anchor="middle">floor pass-rate</text>'.format(
                     y=mt + ph / 2))
    # dots
    for m, s in pts:
        x, y = px(s[field]), py(s["floor_rate"])
        recommended = (m == rec_model_id)
        is_inc = (incumbent and m == incumbent)
        color = "#1a7f37" if recommended else ("#8250df" if is_inc else "#0969da")
        parts.append('<circle cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(x=x, y=y, c=color))
        if recommended:
            parts.append('<circle cx="{x}" cy="{y}" r="11" fill="none" '
                         'stroke="#1a7f37" stroke-width="2"/>'.format(x=x, y=y))
        label = html.escape(m)
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#222" '
                     'text-anchor="middle">{l}</text>'.format(x=x, y=y - 14, l=label))
    # legend
    lg = mt + 6
    parts.append('<circle cx="{x}" cy="{y}" r="5" fill="#1a7f37"/>'
                 '<text x="{tx}" y="{ty}" font-size="11" fill="#333">recommended</text>'.format(
                     x=ml + pw - 150, y=lg, tx=ml + pw - 140, ty=lg + 4))
    if incumbent:
        parts.append('<circle cx="{x}" cy="{y}" r="5" fill="#8250df"/>'
                     '<text x="{tx}" y="{ty}" font-size="11" fill="#333">you are here</text>'.format(
                         x=ml + pw - 150, y=lg + 18, tx=ml + pw - 140, ty=lg + 22))
    parts.append("</svg>")
    return "".join(parts)


def _bars(agg, key, label, fmt, maxval=None):
    rows = list(agg.items())
    if maxval is None:
        vals = [s[key] for _, s in rows if s[key] is not None]
        maxval = max(vals) if vals else 1.0
    maxval = maxval or 1.0
    out = ['<div style="margin:8px 0"><div style="font-weight:600;margin-bottom:6px">'
           + html.escape(label) + "</div>"]
    for m, s in rows:
        v = s[key]
        w = 0 if v is None else max(2, (v / maxval) * 320)
        txt = "n/a" if v is None else fmt(v)
        out.append(
            '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
            '<div style="width:200px;font-size:12px;color:#333;overflow:hidden;'
            'text-overflow:ellipsis;white-space:nowrap">{m}</div>'
            '<div style="height:14px;width:{w:.0f}px;background:#0969da;border-radius:3px"></div>'
            '<div style="font-size:12px;color:#555">{t}</div></div>'.format(
                m=html.escape(m), w=w, t=html.escape(txt)))
    out.append("</div>")
    return "".join(out)


def _health_html(tests, layered):
    parts = ['<h2 style="font-size:15px;color:#333">Suite health</h2>']
    if not layered:
        parts.append('<p style="color:#8a6d00;font-size:13px">Tests were not tagged '
                     'by layer, so no saturation / regression checks were run '
                     '(they need floor / discriminating tags).</p>')
        return "".join(parts)
    saturated, regressions = suite_health(tests)
    if not saturated and not regressions:
        parts.append('<p style="color:#1a7f37;font-size:13px">No issues: no saturated '
                     'discriminating tests, and every model cleared every floor test.</p>')
        return "".join(parts)
    if regressions:
        parts.append('<div style="margin:8px 0"><div style="font-weight:600;color:#b35900;'
                     'margin-bottom:6px">Floor failures (regression or coverage gap)</div>'
                     '<ul style="margin:6px 0 6px 18px;padding:0">')
        for m, test in regressions:
            parts.append('<li style="font-size:13px;color:#333;margin:2px 0">model '
                         '<b>{m}</b> failed <b>{t}</b></li>'.format(
                             m=html.escape(m), t=html.escape(test)))
        parts.append("</ul></div>")
    if saturated:
        parts.append('<div style="margin:8px 0"><div style="font-weight:600;color:#8a6d00;'
                     'margin-bottom:6px">Saturated discriminating tests (no ranking signal; '
                     'harden or retire)</div><ul style="margin:6px 0 6px 18px;padding:0">')
        for test in saturated:
            parts.append('<li style="font-size:13px;color:#333;margin:2px 0">{t}</li>'.format(
                t=html.escape(test)))
        parts.append("</ul></div>")
    return "".join(parts)


def _routing_html(cat_aggs, categorized, bar, disc_bar, optimize="cost"):
    parts = ['<h2 style="font-size:15px;color:#333">Per-category routing</h2>']
    if not categorized:
        parts.append('<p style="color:#8a6d00;font-size:13px">No test carried a '
                     'category, so per-category routing is absent. Tag tests with '
                     'metadata.category to enable it.</p>')
        return "".join(parts)
    routing = route_by_category(cat_aggs, bar, disc_bar, optimize)
    pick = ("fastest by median latency" if optimize == "latency"
            else "cheapest")
    parts.append('<p style="font-size:13px;color:#333">Send each category to the '
                 '{} model that clears the bar within it.</p>'.format(pick))
    parts.append('<table style="border-collapse:collapse;font-size:13px;margin:8px 0">'
                 '<thead><tr>'
                 '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #ddd">category</th>'
                 '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #ddd">recommended model</th>'
                 '<th style="text-align:right;padding:4px 12px 4px 0;border-bottom:1px solid #ddd">cost/test</th>'
                 '<th style="text-align:right;padding:4px 0;border-bottom:1px solid #ddd">latency</th>'
                 '</tr></thead><tbody>')
    for c in sorted(routing):
        model, cpt, lat_s = routing[c]
        if model is None:
            cell = ('<td style="padding:4px 12px 4px 0;color:#b35900;font-weight:600">'
                    'NO MODEL CLEARS THE BAR</td>'
                    '<td style="padding:4px 12px 4px 0;text-align:right;color:#b35900">-</td>'
                    '<td style="padding:4px 0;text-align:right;color:#b35900">-</td>')
        else:
            cell = ('<td style="padding:4px 12px 4px 0">{m}</td>'
                    '<td style="padding:4px 12px 4px 0;text-align:right">{c}</td>'
                    '<td style="padding:4px 0;text-align:right">{l}</td>').format(
                        m=html.escape(model), c=html.escape(fmt_cost(cpt)),
                        l=html.escape(fmt_latency(lat_s)))
        parts.append('<tr><td style="padding:4px 12px 4px 0">{cat}</td>{cell}</tr>'.format(
            cat=html.escape(c), cell=cell))
    parts.append("</tbody></table>")
    unmet = [c for c in sorted(routing) if routing[c][0] is None]
    if unmet:
        parts.append('<p style="color:#b35900;font-size:13px">No model clears the '
                     'bar in: {}. Raise coverage, lower the bar, or add a stronger '
                     'model for these.</p>'.format(html.escape(", ".join(unmet))))
    return "".join(parts)


def render_html(agg, layered, bar, disc_bar, rec_model_id, incumbent, tests=None,
                cat_aggs=None, categorized=False, optimize="cost"):
    pick = ("fastest by median latency" if optimize == "latency"
            else "cheapest")
    rec_line = ("Run <b>{}</b>, the {} model that clears the bar.".format(
        html.escape(rec_model_id), pick) if rec_model_id
        else "No model clears the bar (floor pass-rate at or above {:.0f}%).".format(bar * 100))
    note = ("" if layered else
            '<p style="color:#8a6d00;font-size:13px">Tests were not tagged by layer, '
            'so floor = overall pass-rate and disc = mean score across all tests.</p>')
    frontier = _svg_frontier(agg, rec_model_id, incumbent, optimize)
    passbars = _bars(agg, "floor_rate", "Floor pass-rate", fmt_rate, maxval=1.0)
    costbars = _bars(agg, "cost_per_test", "Cost per test", fmt_cost)
    # In latency mode also show the median-latency bars (the active metric).
    if optimize == "latency":
        costbars += _bars(agg, "latency_s", "Median latency (seconds)", fmt_latency)
    health = _health_html(tests if tests is not None else {}, layered)
    routing = _routing_html(cat_aggs if cat_aggs is not None else {}, categorized,
                            bar, disc_bar, optimize)
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>clawhound model recommendation</title></head>
<body style="font-family:system-ui,sans-serif;max-width:820px;margin:32px auto;color:#111">
<h1 style="font-size:22px">Which model to run</h1>
<p style="font-size:16px">{rec}</p>
{note}
<h2 style="font-size:15px;color:#333">{frontier_title}</h2>
{frontier}
{passbars}
{costbars}
{routing}
{health}
<p style="color:#777;font-size:12px;margin-top:24px">Bar: floor pass-rate at or above {barp:.0f}%{db}. Generated by clawhound from a promptfoo results file.</p>
</body></html>""".format(
        rec=rec_line, note=note, frontier=frontier, passbars=passbars, costbars=costbars,
        routing=routing, health=health, barp=bar * 100,
        frontier_title=("Latency vs quality" if optimize == "latency" else "Cost vs quality"),
        db="" if disc_bar <= 0 else ", disc score at or above {:.2f}".format(disc_bar))


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
    assert ("anthropic:haiku", "never fabricate a score") in regressions, regressions
    # the all-pass floor test must not appear as a regression
    assert all(t != "odds axiom deflate/inflate" for _, t in regressions), regressions

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

    # HTML report must carry both the suite-health and the routing block.
    doc = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                      cat_aggs, categorized)
    assert "Suite health" in doc, "suite-health block missing from HTML"
    assert "diagnose devig longshot bias" in doc, "saturated test missing from HTML"
    assert "Per-category routing" in doc, "routing block missing from HTML"
    assert "NO MODEL CLEARS THE BAR" in doc, "no-clear marking missing from HTML"
    assert "Cost vs quality" in doc, "cost-mode frontier title missing from HTML"
    # latency-mode HTML must relabel the frontier and show the latency metric.
    doc_lat = render_html(agg, layered, 1.0, 0.0, rec_lat, "anthropic:opus", tests,
                          cat_aggs, categorized, optimize="latency")
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
    rec = recommend(agg, args.bar, args.disc_bar, args.optimize)
    print_report(agg, layered, args.bar, args.disc_bar, rec, args.incumbent, args.optimize)
    print_routing(cat_aggs, categorized, args.bar, args.disc_bar, args.optimize)
    print_health(tests, layered)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(agg, layered, args.bar, args.disc_bar, rec,
                            args.incumbent, tests, cat_aggs, categorized, args.optimize))
    print("\nHTML report: " + args.out)


if __name__ == "__main__":
    main()
