"""Which model to run, overall and per category. Reads an already-computed
aggregate (see aggregate.py); never re-parses records itself except where a
routing view needs a fresh regrouping (category_floor_failures)."""

import os

from .parsing import rec_category
from .aggregate import aggregate_tests, suite_health


# ----------------------------------------------------------------------------
# Recommend: cheapest (or fastest) model that clears the bar
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


def exceeds_latency_ceiling(s, latency_ceiling):
    """True when a model's median latency is KNOWN and over the ceiling.

    A ceiling is a practical-viability gate, not a data-quality one: "correct
    but nobody will wait N minutes for an answer" is a real, distinct verdict
    from "wrong" or "too expensive". Unknown latency (None) never counts as
    exceeding it - absence of data is not evidence of slowness. latency_ceiling
    of None or <=0 means no ceiling is set (disabled)."""
    if not latency_ceiling or latency_ceiling <= 0:
        return False
    lat = s.get("latency_s")
    return lat is not None and lat > latency_ceiling


def recommend(agg, bar, disc_bar, optimize="cost", latency_ceiling=None):
    def clears(s):
        if s["floor_rate"] is None or s["floor_rate"] < bar:
            return False
        if disc_bar > 0 and (s["disc"] is None or s["disc"] < disc_bar):
            return False
        if exceeds_latency_ceiling(s, latency_ceiling):
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
# Per-category routing: pick the cheapest (or fastest) model that clears the
# bar WITHIN each category. The result is a routing policy: category -> model
# (Opus for the hard categories, a cheap model for the easy ones). For a
# multi-repo project each service is a category, so this is per-service
# model routing.
# ----------------------------------------------------------------------------

def route_by_category(cat_aggs, bar, disc_bar, optimize="cost", latency_ceiling=None):
    """category -> (recommended model or None, cost_per_test or None, latency_s or None).

    Reuses recommend() so each category uses the same bar / disc_bar gate and
    the same optimize metric as the overall recommendation. A None model means
    no model cleared the bar within that category. Both cost and latency are
    returned when known, regardless of which metric drove the selection, so the
    routing table can show either column.
    """
    routing = {}
    for cat, agg in cat_aggs.items():
        rec = recommend(agg, bar, disc_bar, optimize, latency_ceiling)
        cpt = agg[rec]["cost_per_test"] if (rec and rec in agg) else None
        lat_s = agg[rec]["latency_s"] if (rec and rec in agg) else None
        routing[cat] = (rec, cpt, lat_s)
    return routing


def dual_routing(cat_aggs, bar, disc_bar, latency_ceiling=None):
    """{category: {"cheapest": (model, cpt, lat_s), "fastest": (model, cpt, lat_s)}}.

    Runs the SAME route selection once per metric. The set of models that CLEAR
    the bar is identical for both lenses (the gate is the floor pass-rate, which
    is metric independent); only the tiebreak differs. So cheapest and fastest
    can name DIFFERENT models when more than one model clears within a category,
    and always name the same model when exactly one clears (or none). A latency
    ceiling removes over-ceiling models from BOTH lenses identically - fastest
    being over-ceiling would defeat the point of the fastest lens.
    """
    cost = route_by_category(cat_aggs, bar, disc_bar, "cost", latency_ceiling)
    lat = route_by_category(cat_aggs, bar, disc_bar, "latency", latency_ceiling)
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


def category_route_info(cat, agg, bar, disc_bar, optimize, latency_ceiling=None):
    """Everything the owner line for one category needs.

    Returns dict: model (recommended, or None), n_clearers, strongest (closest
    model when none clears), reason (plain sentence fragment), cpt, lat_s,
    no_floor_tests (True when the bar literally cannot be evaluated here).
    Reasons, none hardcoding a model name:
      "the only model that clears the bar"        exactly one model clears
      "fastest that clears the bar" / "cheapest that clears the bar"
                                                  more than one clears
      "no model clears the bar yet, <strongest> is closest, review the checks"
                                                  models exist, floor tests exist, all failed
      "this category has no floor tests, so the bar can't be evaluated here;
       by discriminating score, <strongest> ranks highest"
                                                  no floor tests in this category at all -
                                                  clears() is unsatisfiable by construction
                                                  (floor_rate is None for every model), which
                                                  is a coverage gap in the SUITE, not a model
                                                  failure - do not render this like a failed bar
    """
    def clears(s):
        if s["floor_rate"] is None or s["floor_rate"] < bar:
            return False
        if disc_bar > 0 and (s["disc"] is None or s["disc"] < disc_bar):
            return False
        if exceeds_latency_ceiling(s, latency_ceiling):
            return False
        return True

    clearers = [m for m, s in agg.items() if clears(s)]
    model = recommend(agg, bar, disc_bar, optimize, latency_ceiling)
    cpt = agg[model]["cost_per_test"] if (model and model in agg) else None
    lat_s = agg[model]["latency_s"] if (model and model in agg) else None
    if model is None:
        strong = strongest_model(agg)
        no_floor_tests = bool(agg) and all(s.get("floor_rate") is None for s in agg.values())
        if no_floor_tests:
            reason = ("this category has no floor tests, so the bar can't be evaluated "
                      "here; by discriminating score, " + str(strong) + " ranks highest")
        else:
            reason = ("no model clears the bar yet, " + str(strong)
                      + " is closest, review the checks")
        return {"model": None, "n_clearers": 0, "strongest": strong,
                "reason": reason, "cpt": None, "lat_s": None,
                "no_floor_tests": no_floor_tests}
    if len(clearers) == 1:
        reason = "the only model that clears the bar"
    else:
        reason = ("fastest that clears the bar" if optimize == "latency"
                  else "cheapest that clears the bar")
    return {"model": model, "n_clearers": len(clearers), "strongest": model,
            "reason": reason, "cpt": cpt, "lat_s": lat_s, "no_floor_tests": False}


def owner_routing(cat_aggs, bar, disc_bar, optimize="cost", latency_ceiling=None):
    """category -> category_route_info, for every category, sorted-friendly."""
    return {cat: category_route_info(cat, agg, bar, disc_bar, optimize, latency_ceiling)
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


# ----------------------------------------------------------------------------
# Benchmark comparison: an incumbent (the benchmark model) vs up to a few named
# challengers, expressed as cost/latency/disc DELTAS rather than raw numbers -
# "should I switch from what I run today" reads off a percent, not a table of
# absolute figures the reader has to subtract in their head.
# ----------------------------------------------------------------------------

def benchmark_deltas(agg, incumbent, compare_ids):
    """Cost/latency/disc deltas for each compare model vs the incumbent
    (the benchmark), in compare_ids order.

    Returns a list of dicts: {"model": id, "cost_pct": float or None,
    "latency_pct": float or None, "disc_delta": float or None}. Positive
    cost_pct/latency_pct means the compare model is MORE expensive/SLOWER
    than the benchmark; positive disc_delta means it scores HIGHER. A None
    field means the incumbent or the compare model is missing that metric -
    not a real zero delta, so callers must render "n/a", never treat it as 0%.
    """
    inc = agg.get(incumbent)
    out = []
    for cid in compare_ids:
        s = agg.get(cid)
        row = {"model": cid, "cost_pct": None, "latency_pct": None, "disc_delta": None}
        if inc and s:
            ic, cc = inc.get("cost_per_test"), s.get("cost_per_test")
            if ic is not None and cc is not None and ic > 0:
                row["cost_pct"] = (cc / ic - 1) * 100
            il, cl = inc.get("latency_s"), s.get("latency_s")
            if il is not None and cl is not None and il > 0:
                row["latency_pct"] = (cl / il - 1) * 100
            idisc, cdisc = inc.get("disc"), s.get("disc")
            if idisc is not None and cdisc is not None:
                row["disc_delta"] = cdisc - idisc
        out.append(row)
    return out


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
