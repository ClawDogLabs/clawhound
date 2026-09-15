"""Built-in sample-results test. No file, no network: hand-built promptfoo-
shaped records that exercise the suite-health block. Run with `--selftest`."""

from .aggregate import aggregate, aggregate_tests, aggregate_by_category, suite_health
from .routing import recommend, route_by_category, owner_routing, everyday_pick, dual_routing
from .charts import _frontier_ylo
from .report import render_html
from .cli import print_report, print_routing, print_health


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

    # --- dual routing: cheapest and fastest computed once per metric. The set
    # of clearers is metric-independent, so where more than one model clears the
    # two lenses can name DIFFERENT models. Frontend is the divergence case. ---
    dr = dual_routing(cat_aggs, 1.0, 0.0)
    assert dr["frontend"]["cheapest"][0] == "anthropic:haiku", dr["frontend"]
    assert dr["frontend"]["fastest"][0] == "anthropic:opus", dr["frontend"]
    # cheapest column carries haiku's per-test cost; fastest carries opus's latency.
    assert dr["frontend"]["cheapest"][1] == 0.002, dr["frontend"]
    assert dr["frontend"]["fastest"][2] == 0.5, dr["frontend"]
    # math: only opus clears, so cheapest and fastest agree (collapse case).
    assert dr["math"]["cheapest"][0] == "anthropic:opus", dr["math"]
    assert dr["math"]["fastest"][0] == "anthropic:opus", dr["math"]
    # theory: nobody clears a floor bar, so neither lens routes a model.
    assert dr["theory"]["cheapest"][0] is None, dr["theory"]
    assert dr["theory"]["fastest"][0] is None, dr["theory"]

    # HTML report must carry the suite-health block, the dual routing table, and
    # BOTH frontier charts under one shared legend.
    doc = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                      cat_aggs, categorized, records=records)
    assert "Suite health" in doc, "suite-health block missing from HTML"
    assert "diagnose devig longshot bias" in doc, "saturated test missing from HTML"
    assert "Per-service routing" in doc, "dual routing table missing from HTML"
    assert "Per-category detail" in doc, "per-category drilldown missing from HTML"
    # two frontier charts: one cost-x, one latency-x, both axis titles present.
    assert "cost vs quality" in doc, "cost frontier title missing from HTML"
    assert "latency vs quality" in doc, "latency frontier title missing from HTML"
    assert "cost per 100 tests (USD)" in doc, "cost x-axis label missing from HTML"
    assert "median latency per test (s)" in doc, "latency x-axis label missing from HTML"
    # exactly ONE shared model legend serves both charts.
    assert doc.count('class="frontier-legend"') == 1, "legend must be shared, not per-chart"
    # dual routing table: a cheapest and a fastest column header.
    assert ">cheapest<" in doc and ">fastest<" in doc, "dual routing columns missing"
    # frontend diverges: the disagreement note must fire and name both picks.
    assert "Cheapest and fastest picks differ" in doc, "disagreement note missing"
    assert "the two lenses disagree here" in doc, "per-category disagreement line missing"
    # math collapses to one model that is both cheapest and fastest.
    assert "cheapest and fastest" in doc, "collapsed same-model routing cell missing"
    # KEEP: legend paragraph, all-models table, incumbent marker, collapse-all.
    assert "you are here" in doc, "incumbent marker missing from HTML"
    assert "NO MODEL CLEARS THE BAR" in doc, "no-clear marking missing from HTML"
    assert "for everyday work" in doc, "owner headline missing from HTML"
    assert "Expand all" in doc and "Collapse all" in doc, "collapse-all control missing"
    assert "<details class=\"cat\"" in doc, "collapsible category detail missing"
    assert "Headline follows your <b>cheapest</b> lens" in doc, "cost-lens headline note missing"

    # --optimize only flips which lens the HEADLINE leads with; both frontiers
    # and both routing picks always show, so the two docs share the same charts.
    doc_lat = render_html(agg, layered, 1.0, 0.0, rec_lat, "anthropic:opus", tests,
                          cat_aggs, categorized, optimize="latency", records=records)
    assert "cost vs quality" in doc_lat and "latency vs quality" in doc_lat, \
        "both frontiers must render regardless of --optimize"
    assert "Per-service routing" in doc_lat, "dual routing must render in latency mode"
    assert "Headline follows your <b>fastest</b> lens" in doc_lat, "latency-lens headline note missing"
    print("\nselftest: OK")
