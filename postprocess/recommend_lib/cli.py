"""Plain-text report printers (the console output alongside the HTML report)."""

from .formatting import fmt_rate, fmt_score, fmt_cost, fmt_latency, fmt_model_name
from .routing import _metric_field, optimize_sort_key, route_by_category
from .aggregate import suite_health


def print_report(agg, layered, bar, disc_bar, rec_model_id, incumbent, optimize="cost"):
    field = _metric_field(optimize)
    rows = sorted(agg.items(), key=optimize_sort_key(optimize))
    display_names = {m: fmt_model_name(m) for m, _ in rows}
    name_w = max([len("model")] + [len(display_names[m]) for m, _ in rows]) + 2
    # Always show cost/test when known; the active metric gets its own column.
    header = "{:<{w}} {:>10} {:>8} {:>14} {:>14}".format(
        "model", "floor", "disc", "cost/100", "latency", w=name_w)
    print(header)
    print("-" * len(header))
    for m, s in rows:
        cpt = s["cost_per_test"]
        mark = ""
        if m == rec_model_id:
            mark = "  <- recommended"
        elif incumbent and m == incumbent:
            mark = "  (you are here)"
        print("{:<{w}} {:>10} {:>8} {:>14} {:>14}{}".format(
            display_names[m], fmt_rate(s["floor_rate"]), fmt_score(s["disc"]),
            fmt_cost(cpt), fmt_latency(s["latency_s"]), mark, w=name_w))
    print()
    if not layered:
        print("Note: tests were not tagged by layer, so floor = overall pass-rate "
              "and disc = mean score across all tests.")
    metric_word = "fastest by median latency" if optimize == "latency" else "lowest cost"
    if rec_model_id:
        s = agg[rec_model_id]
        rec_display = display_names.get(rec_model_id, fmt_model_name(rec_model_id))
        print("Run: {}. Clears the bar (floor {} >= {:.0f}%){} at the {}."
              .format(rec_display, fmt_rate(s["floor_rate"]), bar * 100,
                      "" if disc_bar <= 0 else ", disc {} >= {:.2f}".format(
                          fmt_score(s["disc"]), disc_bar),
                      metric_word))
    else:
        print("No model clears the bar (floor pass-rate >= {:.0f}%{}). "
              "Raise coverage, lower the bar, or add a stronger model."
              .format(bar * 100,
                      "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
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


def print_routing(cat_aggs, categorized, bar, disc_bar, optimize="cost"):
    print()
    title = "Per-category routing"
    print(title)
    print("-" * len(title))
    if not categorized:
        print("No test carried a category, so per-category routing is absent. "
              "Tag tests with metadata.category to enable it.")
        return
    routing = route_by_category(cat_aggs, bar, disc_bar, optimize)
    cats = sorted(routing)
    cat_w = max([len("category")] + [len(c) for c in cats]) + 2
    model_display = {m: fmt_model_name(m) for m, _, _ in routing.values() if m}
    mdl_w = max([len("model")]
                + [len(model_display.get(m, "NO MODEL CLEARS THE BAR")) for m, _, _ in routing.values()]) + 2
    header = "{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
        "category", "model", "cost/100", "latency", cw=cat_w, mw=mdl_w)
    print(header)
    print("-" * len(header))
    for c in cats:
        model, cpt, lat_s = routing[c]
        if model is None:
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, "NO MODEL CLEARS THE BAR", "-", "-", cw=cat_w, mw=mdl_w))
        else:
            display_model = model_display.get(model, fmt_model_name(model))
            print("{:<{cw}} {:<{mw}} {:>14} {:>14}".format(
                c, display_model, fmt_cost(cpt), fmt_latency(lat_s), cw=cat_w, mw=mdl_w))
    print()
    pick = ("fastest by median latency that clears the bar, "
            if optimize == "latency" else "cheapest that clears the bar, ")
    print("Routing policy: send each category to the model shown ({}floor >= {:.0f}%{})."
          .format(pick, bar * 100,
                  "" if disc_bar <= 0 else ", disc >= {:.2f}".format(disc_bar)))
    unmet = [c for c in cats if routing[c][0] is None]
    if unmet:
        print("No model clears the bar in: {}. Raise coverage, lower the bar, "
              "or add a stronger model for these.".format(", ".join(unmet)))
