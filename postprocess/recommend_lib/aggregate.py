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
    rec_completion_tokens, rec_context_exhausted, _median,
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
        })
        a["n"] += 1
        if rec_context_exhausted(r):
            a["ctx_exhausted_n"] += 1
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
            ctoks = rec_completion_tokens(r)
            # Guard div-by-zero and skip the plausibility check when we can't
            # compute a rate at all (no completion-token figure available) -
            # in that case, trust the latency as-is rather than silently drop it.
            implausible = (lat > 0 and ctoks is not None and ctoks > 0
                           and (ctoks / (lat / 1000.0)) > _MAX_PLAUSIBLE_TOKENS_PER_SEC)
            if not implausible:
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
            "ctx_exhausted_n": a["ctx_exhausted_n"],
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
