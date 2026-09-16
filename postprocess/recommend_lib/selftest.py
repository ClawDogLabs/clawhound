"""Built-in sample-results test. No file, no network: hand-built promptfoo-
shaped records that exercise the suite-health block. Run with `--selftest`."""

from .aggregate import aggregate, aggregate_tests, aggregate_by_category, suite_health
from .routing import (
    recommend, route_by_category, owner_routing, everyday_pick, dual_routing,
    exceeds_latency_ceiling, benchmark_deltas,
)
from .charts import _frontier_ylo
from .report import render_html
from .cli import print_report, print_routing, print_health, print_benchmark


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
    # --- category "impossible": a REAL floor test every model fails - distinct
    # from "theory" below (no floor tests at all). This is the genuine "no
    # model clears the bar yet, review the checks" case; must NOT be relabeled
    # as a no-floor-tests coverage gap, since a real floor test exists here and
    # every model failed it. Uses ISOLATED model ids (not opus/haiku) so it
    # doesn't perturb their OVERALL floor rate and the "only opus clears
    # overall" assertions below. -------------------------------------------
    recs.append(rec("anthropic:modelc", "unsolvable floor case", "floor", False, 0.0, 0.02, "impossible", 500))
    recs.append(rec("anthropic:modeld", "unsolvable floor case", "floor", False, 0.0, 0.002, "impossible", 3000))
    # --- category "theory": discriminating-only, so nobody has a floor bar ----
    # A discriminating test EVERY model passes (score >= 0.5): saturated flag.
    recs.append(rec("anthropic:opus", "diagnose devig longshot bias", "discriminating", True, 0.85, 0.02, "theory", 500))
    recs.append(rec("anthropic:haiku", "diagnose devig longshot bias", "discriminating", True, 0.72, 0.002, "theory", 3000))
    # A discriminating test that still SPLITS the models: healthy, must NOT flag.
    recs.append(rec("anthropic:opus", "raw CLV does not prove edge", "discriminating", True, 0.78, 0.02, "theory", 500))
    recs.append(rec("anthropic:haiku", "raw CLV does not prove edge", "discriminating", False, 0.30, 0.002, "theory", 3000))

    # --- context-exhausted case: a record with real completion tokens spent
    # but NO visible output - the model burned its whole budget on hidden
    # reasoning. Distinct from a genuine wrong answer (real output, graded
    # incorrect). Must flag this model with the ctx-exhausted marker. -------
    recs.append({
        "provider": {"id": "anthropic:sonnet"},
        "testCase": {"description": "context exhaustion probe", "metadata": {"layer": "floor", "category": "math"}},
        "success": False, "score": 0.0, "cost": 0.01, "latencyMs": 60000,  # 4000 tok / 60s = ~67 tok/s, a realistic local rate
        "response": {"output": ""},
        "tokenUsage": {"prompt": 90, "completion": 4000, "total": 4090},
    })

    # --- refused case: empty output + nonzero completion tokens, SAME shape
    # as context-exhausted above, but this one is a policy/safety block
    # (response.guardrails.flagged), not a budget problem. Must flag with the
    # refused marker, NOT the ctx-exhausted one - conflating them mislabels a
    # provider refusal as the model running out of room to think. -----------
    recs.append({
        "provider": {"id": "anthropic:fable"},
        "testCase": {"description": "refusal probe", "metadata": {"layer": "floor", "category": "math"}},
        "success": False, "score": 0.0, "cost": 0.01, "latencyMs": 900,
        "response": {"output": "", "finishReason": "content_filter",
                     "guardrails": {"flagged": True, "reason": "cyber"}},
        "tokenUsage": {"prompt": 120, "completion": 52, "total": 172},
    })

    # --- cache-hit latency case: near-zero latency AND completion == 0 (the
    # replay zeroed the token count too), so the tok/s ceiling check alone
    # cannot see it (0 tokens / any time = 0 tok/s, under the ceiling). Only
    # the absolute latency floor catches this shape. success=True so this
    # does not also touch the regression/saturation assertions below. -------
    recs.append({
        "provider": {"id": "anthropic:opus"},
        "testCase": {"description": "cache latency probe", "metadata": {"layer": "floor", "category": "math"}},
        "success": True, "score": 1.0, "cost": 0.0, "latencyMs": 6,
        "response": {"output": "cached answer"},
        "tokenUsage": {"prompt": 50, "completion": 0, "total": 50},
    })

    # --- provider-side cache case (e.g. xAI): REAL output, REAL multi-second
    # latency, but completion tokens report 0 (the whole request landed under
    # a `cached` count instead) so cost computes to 0 too. Unlike the record
    # above, the latency here is genuine and must be KEPT; only cost should
    # be excluded. success=True so it doesn't touch other assertions. -------
    recs.append({
        "provider": {"id": "anthropic:haiku"},
        "testCase": {"description": "provider cache accounting probe", "metadata": {"layer": "floor", "category": "frontend"}},
        "success": True, "score": 1.0, "cost": 0.0, "latencyMs": 2600,
        "response": {"output": "a real, test-specific answer"},
        "tokenUsage": {"prompt": 0, "completion": 0, "cached": 616, "total": 616},
    })

    # --- latency ceiling case: a model that is genuinely CORRECT (clears the
    # floor bar) and genuinely FREE (cheaper than every other model here), but
    # takes 95s per call - a real, distinct verdict from wrong or expensive.
    # Own isolated category ("hardware") and model id so it never perturbs the
    # math/frontend/theory/impossible assertions elsewhere; it competes for
    # the OVERALL cost-mode recommendation though (nothing beats free), which
    # is exactly what proves the ceiling gate does something: without a
    # ceiling this model SHOULD win on cost; with one, it must not. ----------
    recs.append(rec("anthropic:slowmodel", "isolated floor probe", "floor", True, 1.0, 0.0, "hardware", 95000))
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

    # Refused vs context-exhausted must never cross-contaminate: same empty-
    # output-plus-tokens shape, different cause, different marker.
    assert agg["anthropic:fable"]["refused_n"] == 1, agg["anthropic:fable"]
    assert agg["anthropic:fable"]["ctx_exhausted_n"] == 0, agg["anthropic:fable"]
    assert agg["anthropic:sonnet"]["ctx_exhausted_n"] == 1, agg["anthropic:sonnet"]
    assert agg["anthropic:sonnet"]["refused_n"] == 0, agg["anthropic:sonnet"]

    # A cache-hit replay (near-zero latency, cost 0, ctoks 0) must not dilute
    # the cost/latency stats toward "free"/"instant" - opus's real records
    # are all cost 0.02; if the cache record leaked in, cost_per_test would
    # drop to 0.10/6 =~ 0.0167 instead of staying 0.02.
    opus_cpt = agg["anthropic:opus"]["cost_per_test"]
    assert opus_cpt is not None and abs(opus_cpt - 0.02) < 1e-9, opus_cpt

    # A record with real output + real latency but zero completion tokens
    # (provider-side cache accounting, e.g. xAI) must exclude its cost (0.0
    # is not a genuine free response here) while KEEPING its latency sample.
    # Haiku's 3 real records are all cost 0.002; if the fake record's cost=0
    # leaked in, cost_per_test would drop to 0.006/4 = 0.0015 instead of 0.002.
    haiku_cpt = agg["anthropic:haiku"]["cost_per_test"]
    assert haiku_cpt is not None and abs(haiku_cpt - 0.002) < 1e-9, haiku_cpt
    # The 2600ms real latency must still be reflected (not dropped like a
    # true cache hit would be) - haiku's other 3 records are all 3000ms, so
    # a median that moved off 3000 proves the 2600ms sample was counted.
    assert agg["anthropic:haiku"]["latency_s"] is not None

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
    # cost mode with NO ceiling: math->opus, frontend->haiku, hardware->the
    # free-but-95s slowmodel (it's alone in its category, so it wins outright)
    # - a three-way tie at one category each, broken by cheaper overall. Free
    # beats haiku's $0.002, so the tiebreak picks the 95s model as "everyday",
    # which is exactly the wrong-headline problem the latency ceiling exists
    # to fix (see the ceiling assertions below) - without a ceiling, tie-break
    # logic has no way to know 95s is impractical, so this IS correct given
    # the inputs, not a fixture bug.
    everyday = everyday_pick(owner_route, "cost", agg)
    assert everyday == "anthropic:slowmodel", everyday
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

    # --- context-exhausted flag: a record with real completion tokens but no
    # visible output must be counted, and ONLY for the model that has one. ---
    assert agg["anthropic:sonnet"]["ctx_exhausted_n"] == 1, agg["anthropic:sonnet"]
    assert agg["anthropic:opus"]["ctx_exhausted_n"] == 0, agg["anthropic:opus"]
    assert agg["anthropic:haiku"]["ctx_exhausted_n"] == 0, agg["anthropic:haiku"]

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
    # "theory" (no floor tests at all) must render the DISTINCT, calmer label -
    # conflating it with a genuine all-models-failed floor case is the bug
    # this fixture guards against.
    assert "NO FLOOR TESTS HERE" in doc, "no-floor-tests marking missing from HTML"
    # the owner-summary bullet list must ALSO use the distinct "no floor tests
    # here" wording for "theory", never "no model clears it yet" - that phrase
    # implies models were tried and failed, which never happened here (no
    # floor test in this category ever gated anything).
    assert "no floor tests here" in doc, \
        "owner-summary must render the no-floor-tests label, not a generic no-clear one"
    assert '<li class="no-floor">' in doc, "owner-summary no-floor <li> class missing"
    # "impossible" (a real floor test every model failed) is the genuine
    # no-clearer case and must keep the original wording, never softened to
    # the no-floor-tests phrasing.
    assert "no model clears it yet" in doc, \
        "genuine no-clearer wording must still appear for a real failed floor test"
    assert "for everyday work" in doc, "owner headline missing from HTML"
    assert "Expand all" in doc and "Collapse all" in doc, "collapse-all control missing"
    assert "<details class=\"cat\"" in doc, "collapsible category detail missing"
    assert "Headline follows your <b>cheapest</b> lens" in doc, "cost-lens headline note missing"
    # the context-exhausted model must be starred/flagged in the all-models table.
    assert 'class="ctx-warn"' in doc, "ctx-exhausted marker missing from HTML"
    assert "failed to finish 1 test" in doc, "ctx-exhausted tooltip note missing from HTML"

    # --optimize only flips which lens the HEADLINE leads with; both frontiers
    # and both routing picks always show, so the two docs share the same charts.
    doc_lat = render_html(agg, layered, 1.0, 0.0, rec_lat, "anthropic:opus", tests,
                          cat_aggs, categorized, optimize="latency", records=records)
    assert "cost vs quality" in doc_lat and "latency vs quality" in doc_lat, \
        "both frontiers must render regardless of --optimize"
    assert "Per-service routing" in doc_lat, "dual routing must render in latency mode"
    assert "Headline follows your <b>fastest</b> lens" in doc_lat, "latency-lens headline note missing"

    # --- latency ceiling: a correct, genuinely free model that takes 95s must
    # NOT win the overall recommendation once a 90s ceiling is set, even though
    # nothing beats free on cost - correct-but-too-slow is excluded from being
    # RECOMMENDED, but its floor/disc numbers stay real and visible. ----------
    assert exceeds_latency_ceiling(agg["anthropic:slowmodel"], 90) is True, \
        "95s model must exceed a 90s ceiling"
    assert exceeds_latency_ceiling(agg["anthropic:slowmodel"], None) is False, \
        "no ceiling set (None) must never flag anything as exceeding it"
    assert exceeds_latency_ceiling(agg["anthropic:slowmodel"], 0) is False, \
        "ceiling of 0 means disabled, same as None"
    # without a ceiling, the free 95s model DOES win overall cost-mode - proves
    # the fixture is actually cheapest, so the exclusion below is the ceiling
    # doing something, not just losing on cost anyway.
    rec_no_ceiling = recommend(agg, 1.0, 0.0, "cost", latency_ceiling=None)
    assert rec_no_ceiling == "anthropic:slowmodel", rec_no_ceiling
    # with a 90s ceiling, it must be excluded and a real (faster) clearer wins.
    rec_ceiling = recommend(agg, 1.0, 0.0, "cost", latency_ceiling=90)
    assert rec_ceiling != "anthropic:slowmodel", rec_ceiling
    assert rec_ceiling is not None, "a faster clearer must still be found"

    # the HTML report must warn (dagger-marker) the over-ceiling model without
    # hiding its floor/disc numbers, and must NOT recommend it as "run this".
    doc_ceiling = render_html(agg, layered, 1.0, 0.0, rec_ceiling, "anthropic:opus",
                              tests, cat_aggs, categorized, records=records,
                              latency_ceiling=90)
    assert "exceeds your 90s latency ceiling" in doc_ceiling, \
        "latency-ceiling warning missing from HTML"
    assert "100%" in doc_ceiling, "slow model's real floor score must still be shown"

    # --- benchmark comparison: opus is the incumbent, haiku and sonnet are the
    # challengers. haiku is cheaper (-90%), slower (+500%), and weaker on disc
    # (opus 0.815 avg vs haiku 0.51 avg = -0.305). sonnet has only one record
    # (no discriminating layer at all here), so its disc delta must be None,
    # not a false 0 - a missing metric is not a real zero delta. ------------
    bd = benchmark_deltas(agg, "anthropic:opus", ["anthropic:haiku", "anthropic:sonnet"])
    assert len(bd) == 2, bd
    haiku_row, sonnet_row = bd
    assert haiku_row["model"] == "anthropic:haiku", haiku_row
    assert abs(haiku_row["cost_pct"] - -90.0) < 1e-6, haiku_row
    assert abs(haiku_row["latency_pct"] - 500.0) < 1e-6, haiku_row
    assert abs(haiku_row["disc_delta"] - -0.305) < 1e-6, haiku_row
    assert sonnet_row["model"] == "anthropic:sonnet", sonnet_row
    assert abs(sonnet_row["cost_pct"] - -50.0) < 1e-6, sonnet_row
    assert abs(sonnet_row["latency_pct"] - 11900.0) < 1e-6, sonnet_row
    assert sonnet_row["disc_delta"] is None, sonnet_row  # sonnet has no disc data at all

    # a compare id absent from agg entirely must return all-None deltas, not raise.
    bd_missing = benchmark_deltas(agg, "anthropic:opus", ["anthropic:nonexistent"])
    assert bd_missing == [{"model": "anthropic:nonexistent", "cost_pct": None,
                           "latency_pct": None, "disc_delta": None}], bd_missing

    print_benchmark("anthropic:opus", ["anthropic:haiku", "anthropic:sonnet"], bd)

    doc_bench = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                            cat_aggs, categorized, records=records,
                            compare_ids=["anthropic:haiku", "anthropic:sonnet"], deltas=bd)
    assert "Benchmark comparison" in doc_bench, "benchmark section missing from HTML"
    assert "-90%" in doc_bench, "haiku cost delta missing from HTML"
    assert "+500%" in doc_bench, "haiku latency delta missing from HTML"
    # a report with no compare_ids must render no benchmark section at all.
    doc_nobench = render_html(agg, layered, 1.0, 0.0, rec, "anthropic:opus", tests,
                              cat_aggs, categorized, records=records)
    assert "Benchmark comparison" not in doc_nobench, \
        "benchmark section must be absent when no --compare is given"

    print("\nselftest: OK")
