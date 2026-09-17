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


def rec_output_empty(r):
    """Whether a record's visible output is empty ("" or None), defensively.

    Checked at response.output first (the shape seen from Ollama/local
    providers), falling back to a top-level `output` field some provider
    shapes use. A record can be "empty" here while still having spent real
    completion tokens - that combination (see rec_context_exhausted) is the
    signature of a model that burned its whole budget on hidden reasoning and
    never got to write a visible answer, distinct from a genuine wrong answer.
    """
    resp = r.get("response")
    if isinstance(resp, dict) and "output" in resp:
        out = resp.get("output")
        return out is None or out == ""
    out = r.get("output")
    return out is None or out == ""


def rec_refused(r):
    """True when a record's empty output is a policy/safety refusal, not a
    budget problem. Checked defensively across provider response shapes:
    Anthropic's proxy-level guardrails block (response.guardrails.flagged)
    and the finishReason values ("content_filter", "refusal") providers use
    to mark a blocked generation. A refusal can spend a handful of reasoning
    tokens before being blocked, which is exactly rec_context_exhausted's
    signature (empty output + completion tokens > 0) - so this must be
    checked BEFORE calling something "context exhausted", or a real policy
    block gets mislabeled as the model running out of room to think."""
    resp = r.get("response")
    if not isinstance(resp, dict):
        return False
    guardrails = resp.get("guardrails")
    if isinstance(guardrails, dict) and guardrails.get("flagged"):
        return True
    finish_reason = resp.get("finishReason") or resp.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason.lower() in ("content_filter", "refusal"):
        return True
    return False


def rec_context_exhausted(r):
    """True when a record burned real completion tokens but produced NO
    visible output - the model spent its whole generation budget on hidden
    reasoning and never got to write an answer. Distinct from a genuine wrong
    answer (which has real output text that was just graded incorrect), from
    a true API error (which typically reports 0 completion tokens), and from
    a policy refusal (see rec_refused) - a refusal is not a budget problem
    even when it also has empty output and nonzero completion tokens."""
    if rec_refused(r):
        return False
    ctoks = rec_completion_tokens(r)
    return rec_output_empty(r) and ctoks is not None and ctoks > 0


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


def rec_cost_unreliable(r):
    """True when a record has real, non-empty output but reports zero (or
    missing) completion tokens - internally inconsistent, since no real
    generation produces visible text at zero completion tokens. Seen with a
    provider's own server-side prompt caching (e.g. xAI reporting the whole
    request under a `cached` token count, with `completion: 0` and a real,
    test-specific answer and a genuine multi-second latency): the LATENCY is
    real and should be trusted, but the cost computed from a zeroed token
    count is definitionally wrong for that record, not a genuine $0 response.
    Distinct from rec_context_exhausted (empty output + real tokens) and from
    a promptfoo response-cache replay (near-zero latency): this one has real
    output and can have entirely realistic latency, so it must not also
    suppress the record's latency sample the way a cache hit does."""
    ctoks = rec_completion_tokens(r)
    return (not rec_output_empty(r)) and (ctoks is None or ctoks == 0)


def rec_error(r):
    """The record's top-level `error` string, or None. promptfoo sets this
    when a test case did not complete normally: a provider call that threw
    (bad model id, network failure) or a grading/assertion call that threw
    (most commonly seen: the judge provider has no API key configured, so
    every llm-rubric/g-eval assertion errors out). Either way this is an
    INFRASTRUCTURE failure, not a graded answer, and must never be counted
    as a wrong one - a run where the judge has no key looks identical to a
    92% failure rate unless this is checked separately, when in truth every
    one of those models may have answered correctly and simply never got
    graded. Callers exclude error records from floor/disc scoring entirely
    (see aggregate()) while still counting real cost/latency from the
    underlying model call, which can succeed even when grading fails."""
    err = r.get("error")
    return err if isinstance(err, str) and err.strip() else None


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
