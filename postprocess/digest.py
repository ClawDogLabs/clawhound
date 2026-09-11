#!/usr/bin/env python
"""clawhound digest: a human-review view of a promptfoo suite.

Reading dozens of YAML test blocks is not review, it is a slog nobody finishes.
This prints ONE line per test, the rule it checks and (for deterministic asserts)
the concrete literal or expected value, grouped by category and layer, so a human
can scan in a minute whether each service tests the RIGHT set of things, whether
the floor math is right, and above all what is MISSING, without reading prompts.

Two output modes:
  - terminal (default): the one-line-per-test scan described above.
  - --html: a progressive-disclosure page. Collapsed, each test shows a
    plain-language line a product manager or business owner can read
    (metadata.plain); expanded, it shows the technical description, the prompt,
    and the grading, for an engineer. Deterministic render, no LLM involved.

Usage:
  python digest.py <promptfooconfig.yaml>
  python digest.py <promptfooconfig.yaml> --html [--out <path>]
"""

import argparse
import html
import os
import re
import sys

import yaml


def _num_from_js(code):
    """Surface the expected number from a `Math.abs(v - X)` style numeric assert."""
    m = re.search(r"Math\.abs\(\s*v\s*-\s*\(?(-?\d+(?:\.\d+)?)", code or "")
    return m.group(1) if m else None


def _tol_from_js(code):
    """Surface the tolerance from a `... <= T` numeric assert."""
    m = re.search(r"<=\s*(-?\d+(?:\.\d+)?)", code or "")
    return m.group(1) if m else None


def assert_summary(a):
    """One short human phrase for what a single assert checks."""
    if not isinstance(a, dict):
        return str(a)
    t = a.get("type", "?")
    v = a.get("value")
    if t in ("javascript", "python"):
        n = _num_from_js(v if isinstance(v, str) else "")
        return ("= " + n) if n is not None else (t + " check")
    if t == "g-eval":
        k = len(v) if isinstance(v, list) else 1
        return "graded rubric (" + str(k) + " criteria)"
    if t == "llm-rubric":
        return "judged rubric"
    if t in ("contains-all", "contains-any"):
        return t + " " + str(v)
    if t in ("icontains", "contains"):
        return 'contains "' + str(v) + '"'
    if t == "equals":
        return "== " + str(v)
    if t == "regex":
        return "matches /" + str(v) + "/"
    if t in ("not-regex", "not-contains", "icontains-not"):
        return "must NOT contain " + str(v)
    return t


def _det_phrase(t, v):
    """Plain engineer phrasing for a deterministic (non-graded) assert."""
    if t == "equals":
        return 'answer must equal "' + str(v) + '"'
    if t == "contains":
        return 'output must contain "' + str(v) + '"'
    if t == "icontains":
        return 'output must contain "' + str(v) + '" (case-insensitive)'
    if t == "contains-all":
        return "output must contain all of: " + ", ".join(str(x) for x in (v or []))
    if t == "contains-any":
        return "output must contain any of: " + ", ".join(str(x) for x in (v or []))
    if t == "regex":
        s = str(v)
        if s == r"\w":
            return "output must be non-empty"
        return "output must match /" + s + "/"
    if t == "not-regex":
        s = str(v)
        if "\\u2014" in s or "\\u2013" in s or chr(0x2014) in s or chr(0x2013) in s:
            return "output must NOT contain an em dash or en dash"
        return "output must NOT match /" + s + "/"
    if t in ("not-contains", "icontains-not"):
        return 'output must NOT contain "' + str(v) + '"'
    return str(t) + ": " + str(v)


def assert_html(a):
    """Render one assert as an HTML grading block, in plain engineer terms.

    Deterministic asserts become a single monospace check line. g-eval and
    llm-rubric render their criteria / rubric text VERBATIM so the reader sees
    exactly what is graded.
    """
    esc = html.escape
    if not isinstance(a, dict):
        return '<div class="check"><code>' + esc(str(a)) + "</code></div>"
    t = a.get("type", "?")
    v = a.get("value")

    if t in ("javascript", "python"):
        code = v if isinstance(v, str) else ""
        exp = _num_from_js(code)
        tol = _tol_from_js(code)
        if exp is not None:
            if tol in (None, "0"):
                phrase = "answer must equal " + exp
            else:
                phrase = "answer must equal " + exp + " (within " + tol + ")"
        else:
            phrase = "custom " + t + " check on the model's output"
        return '<div class="check"><code>' + esc(phrase) + "</code></div>"

    if t == "g-eval":
        crit = v if isinstance(v, list) else [v]
        items = "".join("<li>" + esc(str(c)) + "</li>" for c in crit)
        return (
            '<div class="check rubric"><code>graded rubric ('
            + str(len(crit))
            + " criteria, all must hold)</code>"
            + '<ul class="criteria">'
            + items
            + "</ul></div>"
        )

    if t == "llm-rubric":
        text = v if isinstance(v, str) else str(v)
        return (
            '<div class="check rubric"><code>judged by rubric</code>'
            + '<blockquote class="rubric-text">'
            + esc(text.strip())
            + "</blockquote></div>"
        )

    return '<div class="check"><code>' + esc(_det_phrase(t, v)) + "</code></div>"


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1f2b; background: #f7f8fa; margin: 0; padding: 2rem 1rem; line-height: 1.5; }
.wrap { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .35rem; }
.summary { color: #333; margin: 0 0 .2rem; font-size: 1rem; }
.framing { color: #667; font-size: .95rem; margin: 0 0 1.4rem; }
.wrapper-note { background: #eef1f6; border: 1px solid #dde2ea; border-radius: 6px;
  padding: .6rem .8rem; font-size: .85rem; color: #333; margin-bottom: 1.4rem; }
.wrapper-note code { background: #fff; }
details.cat { border: 1px solid #d7dce4; border-radius: 8px; background: #fff;
  margin-bottom: 1rem; overflow: hidden; }
details.cat > summary { font-size: 1.12rem; font-weight: 600; padding: .8rem 1rem;
  cursor: pointer; background: #fbfcfe; }
details.cat > summary .count { color: #889; font-weight: 500; }
.cat-desc { display: block; color: #667; font-weight: 400; font-size: .82rem;
  margin: .35rem 0 0; }
.layer { padding: 0 1rem; }
.layer h3 { font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
  color: #667; margin: .9rem 0 .3rem; }
details.test { border-top: 1px solid #eef0f4; }
details.test:first-of-type { border-top: none; }
details.test > summary { padding: .55rem 0; cursor: pointer; list-style: none;
  font-size: .95rem; }
details.test > summary::-webkit-details-marker { display: none; }
details.test > summary::before { content: "\\25B8"; color: #99a; margin-right: .5rem; }
details.test[open] > summary::before { content: "\\25BE"; }
.tag { display: inline-block; font-size: .66rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .04em; padding: .08rem .4rem; border-radius: 4px; margin-left: .45rem;
  vertical-align: middle; }
.tag.floor { background: #e3f6ec; color: #1a7f46; }
.tag.discriminating { background: #fdf0e0; color: #a5631a; }
.tag.unlayered { background: #eceef2; color: #667; }
.body { padding: .25rem 0 .9rem 1.3rem; }
.tech-desc { color: #556; font-size: .9rem; margin: 0 0 .6rem; }
.field-label { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
  color: #889; margin: .7rem 0 .25rem; font-weight: 700; }
pre.prompt { background: #0f1420; color: #e6e9f0; padding: .85rem; border-radius: 6px;
  overflow-x: auto; font-size: .82rem; white-space: pre-wrap; margin: 0; }
.check { margin: .3rem 0; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, Menlo, monospace;
  font-size: .82rem; background: #eef1f6; padding: .05rem .3rem; border-radius: 4px; }
.criteria { margin: .35rem 0 .2rem; padding-left: 1.2rem; }
.criteria li { margin: .22rem 0; font-size: .86rem; color: #333; }
.rubric-text { margin: .35rem 0; padding: .5rem .7rem; border-left: 3px solid #cdd3dd;
  color: #333; font-size: .86rem; background: #fafbfc; white-space: pre-wrap; }
.controls { position: fixed; top: 12px; right: 14px; z-index: 20; display: flex; gap: 6px; }
.controls button { font: inherit; font-size: .78rem; padding: .35rem .7rem; cursor: pointer;
  border: 1px solid #cdd3dd; background: #fff; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.controls button:hover { background: #eef1f6; }
"""

_LAYER_ORDER = ("floor", "discriminating", "unlayered")


def load_category_descs(config_path):
    """Read an OPTIONAL <config-dir>/categories.yaml sidecar.

    Schema: categories: { <name>: { label: <str>, graded_on: <str> } }.
    Returns a dict {category-name: {"label": ..., "graded_on": ...}}. Absent
    file, unreadable file, or malformed content yields {} (never raises), so a
    missing sidecar just means no description lines render.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "categories.yaml")
    if not os.path.isfile(path):
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
        if isinstance(entry, dict):
            out[name] = entry
    return out


def category_desc_text(entry):
    """Plain one-line description for a category, or None when unavailable.

    Text: '<label>: models are graded on <graded_on>.'
    """
    if not isinstance(entry, dict):
        return None
    label = entry.get("label")
    graded_on = entry.get("graded_on")
    if not label or not graded_on:
        return None
    return str(label) + ": models are graded on " + str(graded_on) + "."


def render_html(config_path, tests, groups, prompt_wrapper, cat_descs=None):
    """Deterministic progressive-disclosure HTML for the suite (no LLM involved)."""
    esc = html.escape
    n_tests = len(tests)
    n_cats = len(groups)
    out = []
    out.append("<!doctype html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append("<title>Review digest: " + esc(os.path.basename(config_path)) + "</title>")
    out.append("<style>" + _CSS + "</style>")
    out.append("</head><body>")
    out.append(
        '<div class="controls">'
        '<button onclick="cwAll(true)">Expand all</button>'
        '<button onclick="cwAll(false)">Collapse all</button>'
        "</div>"
    )
    out.append('<div class="wrap">')

    out.append("<h1>Review digest</h1>")
    out.append(
        '<p class="summary">'
        + str(n_tests)
        + " tests across "
        + str(n_cats)
        + " categories."
        + " <code>"
        + esc(os.path.basename(config_path))
        + "</code></p>"
    )
    out.append(
        '<p class="framing">Scan each category: is this the RIGHT set of checks, '
        "is the math right, what is MISSING? Each line below is the plain-language "
        "summary of one test; expand it for the exact prompt and grading.</p>"
    )

    if prompt_wrapper:
        out.append(
            '<div class="wrapper-note">Every prompt below is sent through one shared '
            "wrapper: <code>" + esc(prompt_wrapper) + "</code> "
            "The per-test prompt is the <code>{{input}}</code> shown in each expansion.</div>"
        )

    cat_descs = cat_descs or {}
    for cat in sorted(groups):
        n = sum(len(v) for v in groups[cat].values())
        out.append('<details class="cat">')
        desc_text = category_desc_text(cat_descs.get(cat))
        summary = "<summary>" + esc(cat) + ' <span class="count">(' + str(n) + ")</span>"
        if desc_text:
            summary += '<span class="cat-desc">' + esc(desc_text) + "</span>"
        summary += "</summary>"
        out.append(summary)
        for layer in _LAYER_ORDER:
            ts = groups[cat].get(layer)
            if not ts:
                continue
            out.append('<div class="layer">')
            out.append("<h3>" + esc(layer) + " (" + str(len(ts)) + ")</h3>")
            for t in ts:
                desc = t.get("description", "(no description)")
                meta = t.get("metadata", {}) or {}
                plain = meta.get("plain")
                collapsed = plain if plain else desc
                asserts = t.get("assert", []) or []
                inp = ((t.get("vars", {}) or {}).get("input", "") or "")

                out.append('<details class="test">')
                out.append(
                    "<summary>"
                    + esc(str(collapsed))
                    + '<span class="tag '
                    + esc(layer)
                    + '">'
                    + esc(layer)
                    + "</span></summary>"
                )
                out.append('<div class="body">')
                if plain and str(desc).strip() != str(plain).strip():
                    out.append('<div class="tech-desc">' + esc(str(desc)) + "</div>")
                out.append('<div class="field-label">Prompt (input)</div>')
                out.append('<pre class="prompt">' + esc(str(inp).rstrip()) + "</pre>")
                out.append('<div class="field-label">Grading</div>')
                if asserts:
                    for a in asserts:
                        out.append(assert_html(a))
                else:
                    out.append('<div class="check"><code>(no assert)</code></div>')
                out.append("</div>")  # .body
                out.append("</details>")
            out.append("</div>")  # .layer
        out.append("</details>")  # .cat

    out.append("</div>")
    out.append(
        "<script>function cwAll(o){"
        "document.querySelectorAll('details').forEach(function(d){d.open=o;});}</script>"
    )
    out.append("</body></html>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("config", help="a promptfooconfig.yaml")
    ap.add_argument("--html", action="store_true", help="write a progressive-disclosure HTML view")
    ap.add_argument("--out", default=None, help="HTML output path (default <config-dir>/digest.html)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    tests = (d or {}).get("tests", []) or []
    if not tests:
        sys.exit("no tests found in " + args.config)

    groups = {}
    for t in tests:
        meta = t.get("metadata", {}) or {}
        cat = meta.get("category", "uncategorized")
        layer = meta.get("layer", "unlayered")
        groups.setdefault(cat, {}).setdefault(layer, []).append(t)

    cat_descs = load_category_descs(args.config)

    if args.html:
        prompts = (d or {}).get("prompts", []) or []
        prompt_wrapper = ""
        if prompts:
            p0 = prompts[0]
            prompt_wrapper = p0 if isinstance(p0, str) else str(p0)
        out_path = args.out or os.path.join(
            os.path.dirname(os.path.abspath(args.config)), "digest.html"
        )
        doc = render_html(args.config, tests, groups, prompt_wrapper, cat_descs)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(doc)
        print("wrote " + out_path + " (" + str(len(tests)) + " tests, "
              + str(len(groups)) + " categories)")
        return

    print("Review digest: " + args.config)
    print(str(len(tests)) + " tests across " + str(len(groups)) + " categories.")
    print("Scan each category: is this the RIGHT set of checks, is the math right, "
          "what is MISSING?")
    print("")

    for cat in sorted(groups):
        n = sum(len(v) for v in groups[cat].values())
        print("== " + cat + " (" + str(n) + ") ==")
        desc_text = category_desc_text(cat_descs.get(cat))
        if desc_text:
            print("    " + desc_text)
        for layer in ("floor", "discriminating", "unlayered"):
            ts = groups[cat].get(layer)
            if not ts:
                continue
            print("  [" + layer + "]")
            for t in ts:
                desc = t.get("description", "(no description)")
                asserts = t.get("assert", []) or []
                summ = "; ".join(assert_summary(a) for a in asserts) or "(no assert)"
                print("    - " + desc + "  [" + summ + "]")
        print("")


if __name__ == "__main__":
    main()
