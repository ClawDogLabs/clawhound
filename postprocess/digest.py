#!/usr/bin/env python
"""clawhound digest: a human-review view of a promptfoo suite.

Reading dozens of YAML test blocks is not review, it is a slog nobody finishes.
This prints ONE line per test, the rule it checks and (for deterministic asserts)
the concrete literal or expected value, grouped by category and layer, so a human
can scan in a minute whether each service tests the RIGHT set of things, whether
the floor math is right, and above all what is MISSING, without reading prompts.

Usage:
  python digest.py <promptfooconfig.yaml>
"""

import argparse
import re
import sys

import yaml


def _num_from_js(code):
    """Surface the expected number from a `Math.abs(v - X)` style numeric assert."""
    m = re.search(r"Math\.abs\(\s*v\s*-\s*\(?(-?\d+(?:\.\d+)?)", code or "")
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


def main():
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("config", help="a promptfooconfig.yaml")
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

    print("Review digest: " + args.config)
    print(str(len(tests)) + " tests across " + str(len(groups)) + " categories.")
    print("Scan each category: is this the RIGHT set of checks, is the math right, "
          "what is MISSING?")
    print("")

    for cat in sorted(groups):
        n = sum(len(v) for v in groups[cat].values())
        print("== " + cat + " (" + str(n) + ") ==")
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
