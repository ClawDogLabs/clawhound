"""Aggregate promptfoo records per model and per test.

Two layers of tests are expected (see docs/eval-design-guide.md):
  floor          deterministic must-pass tests. Scored by promptfoo `success`
                 (boolean). The bar is a pass-rate you must clear (default 1.0).
  discriminating graded tests (g-eval / llm-rubric). Scored by promptfoo `score`
                 (0 to 1). Used to rank and, optionally, as a second gate.

A test is assigned to a layer by, in order: testCase.metadata.layer, then
metadata.layer, then vars.layer / vars.__layer. If nothing tags the layers, the
tool falls back to treating every test as floor for the pass-rate and every
score as discriminating, and says so.
"""

from .parsing import (
    rec_model, rec_layer, rec_category, rec_test_key, rec_cost, rec_latency,
    rec_completion_tokens, rec_context_exhausted, rec_refused, rec_error,
    rec_cost_unreliable, _median,
)

# A cache hit re-served by promptfoo (e.g. a restarted suite) carries the
# ORIGINAL response's token count but near-zero latency, since no generation
# happened. That is not real inference time and pollutes the median latency
# for anything the suite got restarted on. No explicit "this was cached" flag
# survives into the results JSON for every provider shape, so the usable
# signal is the record's own implied tokens/sec: no real model - local or
# API - sustains more than a few hundred tokens/sec. Anything above this
# ceiling is excluded from latency stats (score, cost, and pass/fail are still
# counted normally; only the LATENCY sample is dropped for that record).
_MAX_PLAUSIBLE_TOKENS_PER_SEC = 200

# A second cache-hit signature the tok/s check above cannot see: some cache
# replays zero out the completion-token count along with skipping generation,
# so ctoks == 0 and the tok/s ratio computes to 0 - comfortably under the
# ceiling, so the tok/s check alone WRONGLY keeps it. No genuine network round
# trip to any provider (cloud or local) completes in single-digit
# milliseconds regardless of how few tokens come back, so an absolute floor
# catches this shape independent of token count.
_MIN_PLAUSIBLE_LATENCY_MS = 50


def _is_cache_hit(lat, ctoks):
    """True when a record's latency/token-count combination is the signature
    of a promptfoo cache replay, not a real inference call: either an
    implausible tokens/sec ratio (many tokens delivered near-instantly), or
    latency below any real network+inference round trip (catches a replay
    that also zeroed the token count, which the tok/s check alone cannot see
    - 0 tokens over any latency computes to 0 tok/s, under the ceiling). A
    cache hit's cost (typically 0, no new spend on the replay) and latency
    (near-zero, no real generation happened) are both artifacts of the
    replay, not signal about the model's real per-call price or speed -
    excluded from both stats by every caller of this function. Returns False
    when latency is unknown (nothing to judge cache-hit-ness from; trust
    whatever other fields are present rather than guessing).
    """
    if lat is None:
        return False
    if lat < _MIN_PLAUSIBLE_LATENCY_MS:
        return True
    return (lat > 0 and ctoks is not None and ctoks > 0
            and (ctoks / (lat / 1000.0)) > _MAX_PLAUSIBLE_TOKENS_PER_SEC)


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
            "ctx_exhausted_n": 0,
            "refused_n": 0,
            "error_n": 0,
            "errors": {},
            "cache_hit_n": 0,
        })
        a["n"] += 1
        if rec_context_exhausted(r):
            a["ctx_exhausted_n"] += 1
        if rec_refused(r):
            a["refused_n"] += 1
        err = rec_error(r)
        if err:
            a["error_n"] += 1
            a["errors"][err] = a["errors"].get(err, 0) + 1
        layer = rec_layer(r)
        success = bool(r.get("success"))
        score = r.get("score")
        # An errored record (provider or grading call threw) never reflects a
        # real graded outcome - it must not dilute floor_rate/disc toward a
        # false "the model failed" when the truth is "this was never graded".
        if err:
            pass
        elif layered:
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
        lat = rec_latency(r)
        ctoks = rec_completion_tokens(r)
        cache_hit = _is_cache_hit(lat, ctoks)
        c = rec_cost(r)
        # cache_hit excludes both cost and latency (neither is real - no
        # generation happened). rec_cost_unreliable excludes cost ONLY - it
        # fires when real output came back at a real latency but the token
        # count (and therefore cost) is zeroed, a provider-side accounting
        # gap, not a cache replay - so the latency sample stays trustworthy.
        if c is not None and not cache_hit and not rec_cost_unreliable(r):
            a["cost_sum"] += c
            a["cost_n"] += 1
        if lat is not None and not cache_hit:
            a["latencies"].append(lat)
        if cache_hit:
            # Tracked separately from error_n: a cache hit is not a failure of
            # any kind (the model answered fine, promptfoo just replayed an
            # old response instead of calling the API again) - but it means
            # cost/latency for this record are not real numbers. When EVERY
            # record for a model is a cache hit, cost_known/latency_known both
            # end up False ("n/a" in the report) with no cause given unless
            # this is surfaced - which reads identically to "something broke"
            # unless a caller explicitly explains it, exactly like error_n.
            a["cache_hit_n"] += 1

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
            "ctx_exhausted_n": a["ctx_exhausted_n"],
            "refused_n": a["refused_n"],
            "error_n": a["error_n"],
            "errors": a["errors"],
            "cache_hit_n": a["cache_hit_n"],
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

    Pass uses the promptfoo `success` boolean for every layer, floor and
    discriminating alike - which means it respects whatever `threshold` the
    suite author set on a `g-eval`/`llm-rubric` assertion. This used to be a
    hardcoded `score >= 0.5` for discriminating tests, independent of the
    test's own configured threshold; that silently disagreed with a strict
    threshold (a test author sets `threshold: 1`, expecting a 0.9-scoring
    model to fail it, and the saturation check would still call the test
    saturated because 0.9 clears the unrelated 0.5 floor). Untagged tests
    fall back to `success` too and are left out of both health flags (layer
    stays None), which preserves the untagged-layers fallback: no misleading
    saturation/regression calls when nothing tagged the layers.
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
        # promptfoo's own success boolean, for every layer - it already
        # incorporates whatever threshold the assertion was configured with.
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
# Per-category routing: split the records by category, aggregate each category
# with the SAME rules as the overall aggregate. category -> per-model-aggregate.
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
