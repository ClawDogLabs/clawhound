"""Load + parse a promptfoo results file (defensive: promptfoo has shipped a
few output shapes). Schema note: written to the promptfoo EvaluateSummaryV3
output (top-level `results[]`, each with provider.id, success, score, cost,
tokenUsage). Parsing is defensive; validation against a live promptfoo run is
still pending."""

import json


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


def rec_completion_tokens(r):
    """Completion (output) token count for a record, defensively.

    Checked at the top-level `tokenUsage.completion` first, then
    `response.tokenUsage.completion` (the shape some providers/promptfoo
    versions use). Returns None when absent or unparseable - callers treat a
    None the same as "cannot sanity-check this record's latency".
    """
    for holder in (r.get("tokenUsage"),
                   (r.get("response") or {}).get("tokenUsage") if isinstance(r.get("response"), dict) else None):
        if isinstance(holder, dict) and holder.get("completion") is not None:
            try:
                return float(holder["completion"])
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
