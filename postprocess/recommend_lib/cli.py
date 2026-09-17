"""Plain-text report printers (the console output alongside the HTML report)."""

from .formatting import fmt_rate, fmt_score, fmt_cost, fmt_latency, fmt_model_name
from .routing import _metric_field, optimize_sort_key, route_by_category, exceeds_latency_ceiling
from .aggregate import suite_health


def print_report(agg, layered, bar, disc_bar, rec_model_id, incumbent, optimize="cost",
                 latency_ceiling=None):
    field = _metric_field(optimize)
    rows = sorted(agg.items(), key=optimize_sort_key(optimize))
    # Suffix a marker on any model that returned no visible answer at least
    # once, distinguishing WHY: "*" burned its whole budget on hidden
    # reasoning (rec_context_exhausted); "†" was blocked by a provider
    # safety/content filter (rec_refused); "‡" cleared the correctness bar
    # but is too slow to actually run (exceeds_latency_ceiling) - a real,
    # distinct verdict from wrong or too expensive: this model is CORRECT and
    # would be free/cheap, but nobody is waiting this long for an answer on
    # your current hardware. Conflating the three mislabels the actual fix a
    # suite author or reader needs to make.
    def _marker(s):
        mk = ""
        if s.get("ctx_exhausted_n") or 0:
            mk += "*"
        if s.get("refused_n") or 0:
            mk += "†"
        if exceeds_latency_ceiling(s, latency_ceiling):
            mk += "‡"
        if s.get("error_n") or 0:
            mk += "§"
        return mk
    display_names = {m: fmt_model_name(m) + _marker(s) for m, s in rows}
    name_w = max([len("model")] + [len(display_names[m]) for m, _ in rows]) + 2
    # Always show cost/test when known; the active metric gets its own column.
    header = "{:<{w}} {:>10} {:>8} {:>14} {:>14}".format(
        "model", "floor", "disc", "cost/100", "latency", w=name_w)
    print(header)
    print("-" * len(header))
    ctx_warned = []
    refused_warned = []
    slow_warned = []
    error_warned = []
    for m, s in rows:
        cpt = s["cost_per_test"]
        mark = ""
        if m == rec_model_id:
            mark = "  <- recommended"
        elif incumbent and m == incumbent:
            mark = "  (you are here)"
        print("{:<{w}} {:>10} {:>8} {:>14} {:>14}{}".format(
            display_names[m], fmt_rate(s["floor_rate"]), fmt_score(s["disc"]),
            fmt_cost(cpt, m), fmt_latency(s["latency_s"]), mark, w=name_w))
        ctx_n = s.get("ctx_exhausted_n") or 0
        if ctx_n:
            ctx_warned.append((fmt_model_name(m), ctx_n))
        refused_n = s.get("refused_n") or 0
        if refused_n:
            refused_warned.append((fmt_model_name(m), refused_n))
        if exceeds_latency_ceiling(s, latency_ceiling):
            slow_warned.append((fmt_model_name(m), s.get("latency_s")))
        error_n = s.get("error_n") or 0
        if error_n:
            error_warned.append((fmt_model_name(m), error_n, s.get("n") or 0, s.get("errors") or {}))
    print()
    if ctx_warned:
        for name, n in ctx_warned:
            print("* {}: failed to finish {} test{} within the allotted context/thinking "
                  "budget (no visible answer, all budget spent on hidden reasoning)."
                  .format(name, n, "" if n == 1 else "s"))
        print()
    if refused_warned:
        for name, n in refused_warned:
            print("† {}: {} test{} blocked by the provider's safety/content filter "
                  "(no visible answer, not a wrong answer and not a budget problem)."
                  .format(name, n, "" if n == 1 else "s"))
        print()
    if slow_warned:
        for name, lat in slow_warned:
            print("‡ {}: median {} exceeds your {:.0f}s latency ceiling - floor and disc "
                  "scores above are still real, but this is not fast enough to run in "
                  "practice on your current setup; treat it as exploratory only, or as a "
                  "case for a faster inference stack."
                  .format(name, fmt_latency(lat), latency_ceiling))
        print()
    if error_warned:
        for name, n, total, errors in error_warned:
            msgs = ", ".join('"{}" ({}x)'.format(msg, cnt) for msg, cnt in errors.items())
            print("§ {}: {}/{} tests never got graded, an infrastructure error, not a wrong "
                  "answer: {}. Floor/disc above reflect only the tests that DID grade."
                  .format(name, n, total, msgs))
        print()
    if not layered:
        print("Note: tests were not tagged by layer, so floor = overall pass-rate "
              "and disc = mean score across all tests.")
    metric_word = "fastest by median latency" if optimize == "latency" else "lowest cost"
    ceiling_note = (", latency <= {:.0f}s".format(latency_ceiling)
                    if latency_ceiling and latency_ceiling > 0 else "")
    if rec_model_id:
        s = agg[rec_model_id]
        rec_display = display_names.get(rec_model_id, fmt_model_name(rec_model_id))
        print("Run: {}. Clears the bar (floor {} >= {:.0f}%){}{} at the {}."
              .format(rec_display, fmt_rate(s["floor_rate"]), bar * 100,
                      "" if disc_bar <= 0 else ", disc {} >= {:.2f}".format(
                          fmt_score(s["disc"]), disc_bar),
                      ceiling_note, metric_word))
    else:
        print("No model clears the bar (floor pass-rate >= {:.0f}%{}{}). "
              "Raise coverage, lower the bar, or add a stronger model."
              .format(bar * 100,
                      "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar),
                      ceiling_note))
    if incumbent and rec_model_id and incumbent in agg and incumbent != rec_model_id:
        inc = agg[incumbent].get(field)
        rec = agg[rec_model_id].get(field)
        if inc is not None and rec is not None and inc > 0:
            save = (1 - rec / inc) * 100
            better = "faster" if optimize == "latency" else "cheaper"
            unit = "per test" if optimize == "cost" else "in median latency"
            inc_display = display_names.get(incumbent, fmt_model_name(incumbent))
            rec_display = display_names.get(rec_model_id, fmt_model_name(rec_model_id))
            print("Versus your current {}: about {:.0f}% {} {} at or above your bar."
                  .format(inc_display, save, better, unit))


def print_benchmark(incumbent, compare_ids, deltas):
    print()
    title = "Benchmark comparison"
    print(title)
    print("-" * len(title))
    print("Benchmark: " + fmt_model_name(incumbent))
    for cid, d in zip(compare_ids, deltas):
        cost_s = "cost n/a" if d["cost_pct"] is None else "{:+.0f}% cost".format(d["cost_pct"])
        lat_s = "latency n/a" if d["latency_pct"] is None else "{:+.0f}% latency".format(d["latency_pct"])
        disc_s = "disc n/a" if d["disc_delta"] is None else "{:+.2f} disc".format(d["disc_delta"])
        print("{}: {}, {}, {}".format(fmt_model_name(cid), cost_s, lat_s, disc_s))
    print()
    print("Positive cost/latency is worse (more expensive/slower than the "
          "benchmark); positive disc is better (higher quality).")


def print_health(tests, layered):
    print()
    title = "Suite health"
    print(title)
    print("-" * len(title))
    if not layered:
        print("Tests were not tagged by layer, so no saturation / regression "
              "checks were run (they need floor / discriminating tags).")
        return
    saturated, regressions = suite_health(tests)
    if not saturated and not regressions:
        print("No issues: no saturated discriminating tests, and every model "
              "cleared every floor test.")
        return
    for m, test, fails, runs in regressions:
        print('FLOOR FAIL: model {} fails "{}" ({}/{} runs): regression or '
              'coverage gap.'.format(fmt_model_name(m), test, fails, runs))
    for test in saturated:
        print('SATURATED: discriminating test "{}" - all models passed, so it '
              'gives no ranking signal. Harden or retire it.'.format(test))


def print_routing(cat_aggs, categorized, bar, disc_bar, optimize="cost", latency_ceiling=None):
    print()
    title = "Per-category routing"
    print(title)
    print("-" * len(title))
    if not categorized:
        print("No test carried a category, so per-category routing is absent. "
              "Tag tests with metadata.category to enable it.")
        return
    routing = route_by_category(cat_aggs, bar, disc_bar, optimize, latency_ceiling)
    cats = sorted(routing)
    cat_w = max([len("category")] + [len(c) for c in cats]) + 2
    model_display = {m: fmt_model_name(m) for m, _, _ in routing.values() if m}
    mdl_w = max([len("model")]
                + [len(model_display.get(m, "NO MODEL CLEARS THE BAR")) for m, _, _ in routing.values()]) + 2
    header = "{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
        "category", "model", "cost/100", "latency", cw=cat_w, mw=mdl_w)
    print(header)
    print("-" * len(header))
    no_floor_cats = []
    for c in cats:
        model, cpt, lat_s = routing[c]
        if model is None:
            no_floor_tests = bool(cat_aggs[c]) and all(
                s.get("floor_rate") is None for s in cat_aggs[c].values())
            label = "NO FLOOR TESTS HERE" if no_floor_tests else "NO MODEL CLEARS THE BAR"
            if no_floor_tests:
                no_floor_cats.append(c)
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, label, "-", "-", cw=cat_w, mw=mdl_w))
        else:
            display_model = model_display.get(model, fmt_model_name(model))
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, display_model, fmt_cost(cpt, model), fmt_latency(lat_s), cw=cat_w, mw=mdl_w))
    print()
    pick = ("fastest by median latency that clears the bar, "
            if optimize == "latency" else "cheapest that clears the bar, ")
    print("Routing policy: send each category to the model shown ({}floor >= {:.0f}%{})."
          .format(pick, bar * 100,
                  "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
    unmet = [c for c in cats if routing[c][0] is None and c not in no_floor_cats]
    if unmet:
        print("No model clears the bar in: {}. Raise coverage, lower the bar, "
              "or add a stronger model for these.".format(", ".join(unmet)))
    if no_floor_cats:
        print("No floor tests exist in: {}, so the bar can't be evaluated there "
              "- this is a suite coverage gap, not a model failure."
              .format(", ".join(no_floor_cats)))
