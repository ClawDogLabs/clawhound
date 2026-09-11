#!/usr/bin/env python
"""clawhound recommend: turn a promptfoo results file into a decision.

promptfoo runs the evals across every provider and shows you the numbers. It
does not DECIDE. This reads `promptfoo eval --output results.json` and answers
the question a buyer actually has: which model should I run, does it clear my
bar, and what does it cost. It prints a ranked table and a recommendation, and
writes a self-contained HTML report whose centerpiece is a cost-vs-quality
frontier (inline SVG, no external libraries, opens in any browser).

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
                       [--incumbent provider:model] [--out report.html]
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


def rec_cost(r):
    c = r.get("cost")
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


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

    out = {}
    for m, a in models.items():
        floor_rate = (a["floor_pass"] / a["floor_n"]) if a["floor_n"] else None
        disc = (a["disc_sum"] / a["disc_n"]) if a["disc_n"] else None
        # cost per test uses the count of records that reported a cost
        cpt = (a["cost_sum"] / a["cost_n"]) if a["cost_n"] else None
        out[m] = {
            "floor_rate": floor_rate,
            "disc": disc,
            "cost_per_test": cpt,
            "cost_known": a["cost_n"] > 0,
            "n": a["n"],
        }
    return out, layered


# ----------------------------------------------------------------------------
# Recommend: cheapest model that clears the bar
# ----------------------------------------------------------------------------

def recommend(agg, bar, disc_bar):
    def clears(s):
        if s["floor_rate"] is None or s["floor_rate"] < bar:
            return False
        if disc_bar > 0 and (s["disc"] is None or s["disc"] < disc_bar):
            return False
        return True

    passers = [(m, s) for m, s in agg.items() if clears(s)]
    if not passers:
        return None
    # Cheapest first. Models with unknown cost (local / self-hosted) sort last,
    # since we cannot claim they are cheapest without a number.
    def cost_key(item):
        cpt = item[1]["cost_per_test"]
        return (cpt is None, cpt if cpt is not None else 0.0)
    passers.sort(key=cost_key)
    return passers[0][0]


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


def print_report(agg, layered, bar, disc_bar, rec_model_id, incumbent):
    rows = sorted(
        agg.items(),
        key=lambda kv: (kv[1]["cost_per_test"] is None,
                        kv[1]["cost_per_test"] if kv[1]["cost_per_test"] is not None else 0.0),
    )
    name_w = max([len("model")] + [len(m) for m in agg]) + 2
    header = "{:<{w}} {:>10} {:>8} {:>14} {:>14}".format(
        "model", "floor", "disc", "cost/test", "cost/1k", w=name_w)
    print(header)
    print("-" * len(header))
    for m, s in rows:
        cpt = s["cost_per_test"]
        c1k = None if cpt is None else cpt * 1000
        mark = ""
        if m == rec_model_id:
            mark = "  <- recommended"
        elif incumbent and m == incumbent:
            mark = "  (you are here)"
        print("{:<{w}} {:>10} {:>8} {:>14} {:>14}{}".format(
            m, fmt_rate(s["floor_rate"]), fmt_score(s["disc"]),
            fmt_cost(cpt), fmt_cost(c1k), mark, w=name_w))
    print()
    if not layered:
        print("Note: tests were not tagged by layer, so floor = overall pass-rate "
              "and disc = mean score across all tests.")
    if rec_model_id:
        s = agg[rec_model_id]
        print("Run: {}. Clears the bar (floor {} >= {:.0f}%){} at the lowest cost."
              .format(rec_model_id, fmt_rate(s["floor_rate"]), bar * 100,
                      "" if disc_bar <= 0 else ", disc {} >= {:.2f}".format(
                          fmt_score(s["disc"]), disc_bar)))
    else:
        print("No model clears the bar (floor pass-rate >= {:.0f}%{}). "
              "Raise coverage, lower the bar, or add a stronger model."
              .format(bar * 100,
                      "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
    if incumbent and rec_model_id and incumbent in agg and incumbent != rec_model_id:
        inc = agg[incumbent]["cost_per_test"]
        rec = agg[rec_model_id]["cost_per_test"]
        if inc is not None and rec is not None and inc > 0:
            save = (1 - rec / inc) * 100
            print("Versus your current {}: about {:.0f}% cheaper per test at or above your bar."
                  .format(incumbent, save))


# ----------------------------------------------------------------------------
# HTML report with inline SVG frontier (no external libraries)
# ----------------------------------------------------------------------------

def _svg_frontier(agg, rec_model_id, incumbent):
    pts = [(m, s) for m, s in agg.items() if s["cost_per_test"] is not None]
    W, H = 720, 420
    ml, mr, mt, mb = 70, 30, 30, 60
    pw, ph = W - ml - mr, H - mt - mb
    if not pts:
        return ('<svg width="{w}" height="80"><text x="10" y="45" '
                'font-family="system-ui" font-size="14">No models reported a cost; '
                'nothing to plot on the cost axis.</text></svg>').format(w=W)
    max_cost = max(s["cost_per_test"] for _, s in pts) or 1e-9
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
    # x labels (cost)
    for i in range(0, 5):
        c = max_cost * i / 4.0
        x = px(c)
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#555" '
                     'text-anchor="middle">${v:.4f}</text>'.format(x=x, y=mt + ph + 18, v=c))
    parts.append('<text x="{x}" y="{y}" font-size="12" fill="#333" '
                 'text-anchor="middle">cost per test (USD)</text>'.format(
                     x=ml + pw / 2, y=H - 12))
    parts.append('<text transform="translate(16,{y}) rotate(-90)" font-size="12" '
                 'fill="#333" text-anchor="middle">floor pass-rate</text>'.format(
                     y=mt + ph / 2))
    # dots
    for m, s in pts:
        x, y = px(s["cost_per_test"]), py(s["floor_rate"])
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


def render_html(agg, layered, bar, disc_bar, rec_model_id, incumbent):
    rec_line = ("Run <b>{}</b>, the cheapest model that clears the bar.".format(
        html.escape(rec_model_id)) if rec_model_id
        else "No model clears the bar (floor pass-rate at or above {:.0f}%).".format(bar * 100))
    note = ("" if layered else
            '<p style="color:#8a6d00;font-size:13px">Tests were not tagged by layer, '
            'so floor = overall pass-rate and disc = mean score across all tests.</p>')
    frontier = _svg_frontier(agg, rec_model_id, incumbent)
    passbars = _bars(agg, "floor_rate", "Floor pass-rate", fmt_rate, maxval=1.0)
    costbars = _bars(agg, "cost_per_test", "Cost per test", fmt_cost)
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>clawhound model recommendation</title></head>
<body style="font-family:system-ui,sans-serif;max-width:820px;margin:32px auto;color:#111">
<h1 style="font-size:22px">Which model to run</h1>
<p style="font-size:16px">{rec}</p>
{note}
<h2 style="font-size:15px;color:#333">Cost vs quality</h2>
{frontier}
{passbars}
{costbars}
<p style="color:#777;font-size:12px;margin-top:24px">Bar: floor pass-rate at or above {barp:.0f}%{db}. Generated by clawhound from a promptfoo results file.</p>
</body></html>""".format(
        rec=rec_line, note=note, frontier=frontier, passbars=passbars, costbars=costbars,
        barp=bar * 100,
        db="" if disc_bar <= 0 else ", disc score at or above {:.2f}".format(disc_bar))


def main():
    ap = argparse.ArgumentParser(prog="recommend")
    ap.add_argument("results", help="promptfoo results JSON (promptfoo eval --output)")
    ap.add_argument("--bar", type=float, default=1.0,
                    help="floor pass-rate a model must clear (default 1.0)")
    ap.add_argument("--disc-bar", type=float, default=0.0,
                    help="optional minimum discriminating score (default 0, off)")
    ap.add_argument("--incumbent", default=None,
                    help="provider:model you run today, marked 'you are here'")
    ap.add_argument("--out", default="report.html", help="HTML report path")
    args = ap.parse_args()

    if not os.path.exists(args.results):
        sys.exit("results file not found: " + args.results)
    records = load_records(args.results)
    if not records:
        sys.exit("no evaluation records found in " + args.results)

    agg, layered = aggregate(records)
    rec = recommend(agg, args.bar, args.disc_bar)
    print_report(agg, layered, args.bar, args.disc_bar, rec, args.incumbent)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(agg, layered, args.bar, args.disc_bar, rec, args.incumbent))
    print("\nHTML report: " + args.out)


if __name__ == "__main__":
    main()
