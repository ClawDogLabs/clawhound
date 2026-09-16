"""Self-contained HTML report: CSS, JS, table/section builders, and the
render_html() entrypoint that assembles them into one page. No external
libraries - the frontier charts are inline SVG (see charts.py) and the
interactivity is a small vanilla-JS IIFE embedded below."""

import html

from .formatting import fmt_rate, fmt_score, fmt_cost, fmt_cost_u, fmt_latency, fmt_model_name
from .routing import (
    category_label, strongest_model, dual_routing, owner_routing, everyday_pick,
    category_floor_failures, recommend, exceeds_latency_ceiling,
)
from .aggregate import suite_health
from .charts import _model_colors, _frontier_ylo, _svg_frontier_plot, _frontier_legend_html


# Approximate list prices ($ per 1M input, $ per 1M output) for models we know,
# used ONLY to estimate GRADING spend from the judge's grading tokens: promptfoo
# prices generation itself but does not price grading. Provider/generation cost
# always comes from promptfoo's own per-record figure, so a missing entry here
# never affects it; it only means the judge's grading cost shows "not auto-priced"
# - an honest gap, not a wrong number, for any judge not in this table.
#
# This table is NOT live-fetched (recommend.py stays offline/deterministic -
# no network call belongs in a report-generation hot path over a static
# results.json). Instead it's refreshed as a separate, occasional step: before
# trusting a grading-cost estimate on a suite with a new judge, or periodically
# (a quarter is a reasonable cadence), an AGENT session re-verifies every price
# below against current provider docs/pricing pages (WebSearch/WebFetch - a
# hardcoded scraper is not robust against pricing-page format changes) and
# updates this table with the verification date. Keyed by a substring of the
# model id.
#
# Last fully re-verified: 2026-09-16 (all entries below checked against
# provider pricing pages this pass, not carried over from an older date).
_PRICE_PER_M = {
    "claude-opus-4-8": (5, 25), "claude-opus-5": (5, 25),
    "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25),
    "claude-sonnet-5": (3, 15), "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5), "claude-fable-5": (10, 50),
    "gpt-6-astra": (10, 50),
    "gemini-3.6-flash": (1.50, 7.50), "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.1-pro-preview": (2, 12),
    "gpt-5.6-terra": (2, 12), "gpt-5.6-sol": (4, 20),
    "grok-4.3": (1.25, 2.50), "grok-4.5": (2, 6), "grok-4.6": (2, 6),
}


_REPORT_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1f2b; background: #f7f8fa; margin: 0; padding: 2rem 1rem; line-height: 1.5; }
.wrap { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
h2 { font-size: 1.05rem; margin: 1.8rem 0 .6rem; color: #2a3140; }
.owner { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: 1rem 1.15rem; margin: 0 0 1rem; }
.owner .headline { font-size: 1.05rem; color: #1a1f2b; margin: 0 0 .7rem; }
.owner .headline b { color: #0f5a2a; }
.owner ul { margin: .3rem 0 0; padding: 0; list-style: none; }
.owner li { font-size: .95rem; color: #333; padding: .28rem 0;
  border-top: 1px solid #eef0f4; }
.owner li:first-child { border-top: none; }
.owner li .cat { font-weight: 600; }
.owner li .mdl { color: #0f5a2a; font-weight: 600; }
.owner li .why { color: #778; font-size: .86rem; }
.owner li.none .mdl { color: #b35900; }
.note { color: #8a6d00; font-size: .86rem; margin: .4rem 0; }
.chart { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: .6rem; overflow-x: auto; }
.chart svg { display: block; max-width: 100%; height: auto; margin: 0 auto; }
.chart-title { font-size: .85rem; font-weight: 600; color: #2a3140;
  text-align: center; padding: .1rem 0 .2rem; }
.frontier-row { display: flex; flex-wrap: wrap; gap: .7rem; }
.frontier-row > .chart { flex: 1 1 360px; min-width: 280px; }
.frontier-legend { background: #fff; border: 1px solid #d7dce4; border-radius: 10px;
  padding: .6rem .8rem; margin: .2rem 0 .6rem; }
.frontier-legend .fl-models { display: flex; flex-wrap: wrap; gap: .45rem 1.1rem; }
.legend-item { font-size: .84rem; color: #222; display: inline-flex; align-items: center; }
.legend-item .swatch { width: 11px; height: 11px; border-radius: 50%; display: inline-block;
  margin-right: .35rem; box-shadow: 0 0 0 1px #ccd; }
.legend-item .fl-metrics { color: #778; margin-left: .35rem; }
.legend-item .fl-tag { color: #1a7f37; font-weight: 600; }
.fl-key { font-size: .78rem; color: #889; margin-top: .55rem; }
.fl-key .k-ring { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
  vertical-align: middle; margin-right: .2rem; }
.fl-key .k-ring.win { border: 2px solid #1a7f37; }
.fl-key .k-ring.inc { border: 2px dashed #8250df; }
table.routing { border-collapse: collapse; font-size: .88rem; width: 100%; margin: .3rem 0 .6rem; }
table.routing th { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; }
table.routing td { padding: .4rem .6rem; border-bottom: 1px solid #f0f2f6; vertical-align: top; }
table.routing td .mdl { color: #0f5a2a; font-weight: 600; }
table.routing td .rt-metric { color: #778; font-size: .84rem; }
table.routing td .rt-note { color: #8250df; font-size: .8rem; font-weight: 600; }
table.routing tr.none td { color: #b35900; }
table.routing tr.no-floor td { color: #556; }
.lens-note { color: #667; font-size: .84rem; margin: .2rem 0 .3rem; }
.disagree-note { color: #8a6d00; font-size: .88rem; margin: .35rem 0; }
.agree-note { color: #4a8a5a; font-size: .84rem; margin: .35rem 0; }
table.legend-table { border-collapse: collapse; font-size: .83rem; color: #4a5568; width: 100%;
  background: #f3f5f8; border: 1px solid #e2e6ec; border-radius: 6px; margin: .4rem 0 .9rem;
  overflow: hidden; }
table.legend-table td { padding: .4rem .75rem; border-top: 1px solid #e2e6ec; line-height: 1.5;
  vertical-align: top; }
table.legend-table tr:first-child td { border-top: none; }
table.legend-table td:first-child { white-space: nowrap; width: 1%; color: #2a3140; }
table.models { border-collapse: collapse; font-size: .86rem; width: 100%; margin: .2rem 0; }
table.models th { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; white-space: nowrap; }
table.models th.l, table.models td.l { text-align: left; }
table.models td { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #f0f2f6;
  white-space: nowrap; }
table.models tr.win td { background: #f0faf3; }
table.models td .win-tag { color: #1a7f37; font-weight: 700; font-size: .78rem; }
table.models td .here-tag { color: #8250df; font-weight: 700; font-size: .78rem; }
table.models td .ctx-warn { color: #b91c1c; font-weight: 600; cursor: help; }
details.cat { border: 1px solid #d7dce4; border-radius: 8px; background: #fff;
  margin-bottom: .7rem; overflow: hidden; }
details.cat > summary { font-size: 1rem; padding: .7rem .9rem; cursor: pointer;
  background: #fbfcfe; list-style: none; }
details.cat > summary::-webkit-details-marker { display: none; }
details.cat > summary::before { content: "\\25B8"; color: #99a; margin-right: .5rem; }
details.cat[open] > summary::before { content: "\\25BE"; }
details.cat > summary .cat { font-weight: 600; }
details.cat > summary .arrow { color: #889; }
details.cat > summary .mdl { color: #0f5a2a; font-weight: 600; }
details.cat > summary .why { color: #778; font-size: .82rem; margin-left: .3rem; }
details.cat[data-none="1"] > summary .mdl { color: #b35900; }
details.cat[data-no-floor="1"] > summary .mdl { color: #556; }
.cat-body { padding: .3rem .9rem .9rem; }
.fail-head { font-weight: 600; color: #b35900; font-size: .84rem; margin: .8rem 0 .3rem; }
ul.fails { margin: .2rem 0 .2rem 1.1rem; padding: 0; }
ul.fails li { font-size: .86rem; color: #333; margin: .18rem 0; }
ul.fails li b { color: #1a1f2b; }
.clean { color: #1a7f37; font-size: .86rem; margin: .5rem 0 .2rem; }
ul.sat { margin: .2rem 0 .2rem 1.1rem; padding: 0; }
ul.sat li { font-size: .86rem; color: #333; margin: .18rem 0; }
.foot { color: #889; font-size: .8rem; margin-top: 1.8rem; }
.controls { position: fixed; top: 12px; right: 14px; z-index: 20; display: flex; gap: 6px; }
.controls button { font: inherit; font-size: .78rem; padding: .35rem .7rem; cursor: pointer;
  border: 1px solid #cdd3dd; background: #fff; border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.controls button:hover { background: #eef1f6; }
/* interactivity: cross-highlight (legend <-> both charts <-> table) + tooltip + sort */
.legend-item { cursor: pointer; padding: .12rem .3rem; border-radius: 5px;
  transition: opacity .1s, background .1s; }
.legend-item.cw-on { background: #eef4ff; }
.cw-dot { cursor: pointer; }
.cw-dot circle.dot { transition: opacity .1s; }
body.cw-hl .cw-dot:not(.cw-on) { opacity: .15; }
body.cw-hl .legend-item:not(.cw-on) { opacity: .32; }
body.cw-hl table.models tbody tr:not(.cw-on) { opacity: .34; }
.cw-dot.cw-on circle.dot { stroke: #1a1f2b; stroke-width: 2.5; }
table.models tbody tr { cursor: pointer; }
table.models tbody tr.cw-on { outline: 2px solid #0969da; outline-offset: -2px; }
.cw-tip { position: fixed; z-index: 50; background: #1a1f2b; color: #fff;
  font-size: .76rem; padding: .38rem .55rem; border-radius: 6px; pointer-events: none;
  line-height: 1.4; box-shadow: 0 2px 10px rgba(0,0,0,.28); max-width: 260px; }
.sort-hint { font-size: .78rem; color: #889; margin: .1rem 0 .45rem; }
table.models th.sortable { cursor: pointer; user-select: none; }
table.models th.sortable:hover { color: #0969da; }
table.models th.sortable::after { content: " \\2195"; color: #c4cbd6; font-weight: 400; }
table.models th.sortable.sorted[data-dir="desc"]::after { content: " \\25BC"; color: #0969da; }
table.models th.sortable.sorted[data-dir="asc"]::after { content: " \\25B2"; color: #0969da; }
table.runcost { border-collapse: collapse; font-size: .84rem; width: 100%; margin: .3rem 0 .5rem; }
table.runcost th { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #dde2ea;
  color: #667; font-weight: 600; white-space: nowrap; }
table.runcost th.l, table.runcost td.l { text-align: left; }
table.runcost td { text-align: right; padding: .3rem .6rem; border-bottom: 1px solid #f0f2f6;
  white-space: nowrap; }
table.runcost tr.subtotal td, table.runcost tr.total td { font-weight: 700;
  border-top: 2px solid #dde2ea; background: #f8fafc; }
table.runcost tr.grading td { color: #6b3fb0; }
.runcost-head { font-size: .95rem; color: #1a1f2b; margin: .2rem 0 .55rem; }
.runcost-head b { color: #0f5a2a; }
.runcost-note { font-size: .78rem; color: #889; margin: .35rem 0; }
"""


# Interactivity, kept out of the .format() template so its many braces need no
# escaping. Injected as {script}. cwAll = expand/collapse; the IIFE wires
# cross-highlight (legend <-> both charts <-> table), the dot tooltip, and the
# sortable all-models table. Cross-highlight keys on data-model, present on every
# legend item, chart dot (as <g class="cw-dot">), and table row.
_REPORT_JS = """
function cwAll(o){document.querySelectorAll('details').forEach(function(d){d.open=o;});}
(function(){
  var pinned=null;
  function nodesFor(model){return Array.prototype.filter.call(document.querySelectorAll('[data-model]'),function(e){return e.getAttribute('data-model')===model;});}
  function clearHi(){document.body.classList.remove('cw-hl');Array.prototype.forEach.call(document.querySelectorAll('.cw-on'),function(e){e.classList.remove('cw-on');});}
  function setHi(model){clearHi();document.body.classList.add('cw-hl');nodesFor(model).forEach(function(e){e.classList.add('cw-on');});}
  Array.prototype.forEach.call(document.querySelectorAll('[data-model]'),function(el){
    var model=el.getAttribute('data-model');
    el.addEventListener('mouseenter',function(){if(!pinned)setHi(model);});
    el.addEventListener('mouseleave',function(){if(!pinned)clearHi();});
    el.addEventListener('click',function(){if(pinned===model){pinned=null;clearHi();}else{pinned=model;setHi(model);}});
  });
  var tip=document.createElement('div');tip.className='cw-tip';tip.style.display='none';document.body.appendChild(tip);
  function show(d,e){tip.innerHTML='<b>'+d.getAttribute('data-model')+'</b><br>floor '+d.getAttribute('data-floor')+' \\u00b7 '+d.getAttribute('data-metric')+' '+d.getAttribute('data-xlabel');tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';}
  Array.prototype.forEach.call(document.querySelectorAll('.cw-dot'),function(d){
    d.addEventListener('mouseenter',function(e){show(d,e);});
    d.addEventListener('mousemove',function(e){show(d,e);});
    d.addEventListener('mouseleave',function(){tip.style.display='none';});
  });
  Array.prototype.forEach.call(document.querySelectorAll('table.models th.sortable'),function(th){
    th.addEventListener('click',function(){
      var tb=th.closest('table').querySelector('tbody'),idx=+th.getAttribute('data-col'),better=th.getAttribute('data-better'),cur=th.getAttribute('data-dir');
      var dir=cur?(cur==='asc'?'desc':'asc'):(better==='hi'?'desc':'asc');
      Array.prototype.forEach.call(th.parentNode.children,function(o){o.removeAttribute('data-dir');o.classList.remove('sorted');});
      th.setAttribute('data-dir',dir);th.classList.add('sorted');
      var rows=Array.prototype.slice.call(tb.querySelectorAll('tr'));
      rows.sort(function(a,b){var av=parseFloat(a.children[idx].getAttribute('data-sort')),bv=parseFloat(b.children[idx].getAttribute('data-sort'));if(isNaN(av))av=-1e15;if(isNaN(bv))bv=-1e15;return dir==='asc'?av-bv:bv-av;});
      rows.forEach(function(r){tb.appendChild(r);});
    });
  });
})();
"""


def _model_table(agg, winner, incumbent, latency_ceiling=None):
    """Compact per-model table: floor pass-rate, discriminating score, latency,
    cost, with the winner (recommended for this scope) and incumbent marked."""
    esc = html.escape
    rows = sorted(agg.items(), key=lambda kv: (kv[1]["floor_rate"] is None,
                                               -(kv[1]["floor_rate"] or 0)))
    out = ['<p class="sort-hint">Click a column to sort (best first; click again to '
           'flip). Hover a row to highlight that model in the charts.</p>'
           '<table class="models"><thead><tr>'
           '<th class="l">model</th>'
           '<th class="sortable" data-col="1" data-better="hi">floor</th>'
           '<th class="sortable" data-col="2" data-better="hi">disc</th>'
           '<th class="sortable" data-col="3" data-better="lo">latency</th>'
           '<th class="sortable" data-col="4" data-better="lo">cost /100</th>'
           '<th class="l"></th>'
           '</tr></thead><tbody>']
    for m, s in rows:
        is_win = (m == winner)
        is_here = (incumbent and m == incumbent)
        tag = ('<span class="win-tag">run this</span>' if is_win
               else ('<span class="here-tag">you are here</span>' if is_here else ""))
        fr_v, d_v, lat_v, c_v = (s["floor_rate"], s["disc"],
                                 s["latency_s"], s["cost_per_test"])
        ctx_n = s.get("ctx_exhausted_n") or 0
        refused_n = s.get("refused_n") or 0
        too_slow = exceeds_latency_ceiling(s, latency_ceiling)
        # Three distinct no-good-answer-in-practice modes, never conflated: a
        # budget problem (ctx_n, burned the whole generation on hidden
        # reasoning), a provider safety/content-filter block (refused_n, not
        # a budget or correctness problem at all), and a model that is
        # CORRECT but too slow to run in practice (too_slow) - a real,
        # distinct verdict, not a data-quality problem.
        notes = []
        if ctx_n:
            notes.append(('failed to finish {n} test{ss} within the allotted context/thinking '
                    'budget (burned the whole generation on hidden reasoning and '
                    'returned no visible answer)').format(n=ctx_n, ss="" if ctx_n == 1 else "s"))
        if refused_n:
            notes.append(('{n} test{ss} blocked by the provider\'s safety/content filter '
                    '(no visible answer, not a wrong answer and not a budget problem)'
                    ).format(n=refused_n, ss="" if refused_n == 1 else "s"))
        if too_slow:
            notes.append(('median {lat} exceeds your {ceil:.0f}s latency ceiling - correct, '
                    'floor and disc scores are real, but not fast enough to run in '
                    'practice on your current setup').format(
                        lat=fmt_latency(lat_v), ceil=latency_ceiling))
        if notes:
            marker = ("*" if ctx_n else "") + ("†" if refused_n else "") + ("‡" if too_slow else "")
            name_html = ('<span class="ctx-warn" title="{note}">{display_m}{marker}'
                        '</span>').format(note=esc("; ".join(notes)),
                                          display_m=esc(fmt_model_name(m)), marker=marker)
        else:
            name_html = esc(fmt_model_name(m))
        out.append(
            '<tr class="{cls}" data-model="{m}"><td class="l">{display_m}</td>'
            '<td data-sort="{frs}">{fr}</td><td data-sort="{ds}">{d}</td>'
            '<td data-sort="{ls}">{lat}</td><td data-sort="{cs}">{c}</td>'
            '<td class="l">{tag}</td></tr>'.format(
                cls="win" if is_win else "", m=esc(m), display_m=name_html,
                frs=(fr_v if fr_v is not None else -1),
                ds=(d_v if d_v is not None else -1),
                ls=(lat_v if lat_v is not None else 1e15),
                cs=(c_v if c_v is not None else 1e15),
                fr=esc(fmt_rate(fr_v)), d=esc(fmt_score(d_v)),
                lat=esc(fmt_latency(lat_v)), c=esc(fmt_cost(c_v, m)), tag=tag))
    out.append("</tbody></table>")
    return "".join(out)


def _owner_summary_html(lead_route, route_cost, route_lat, everyday, labels, optimize):
    """Always-visible, plain-language routing summary an owner can read.

    Headline leads with the --optimize lens (`lead_route` is the routing for that
    lens): the everyday pick (recommended for the most categories) and the
    exceptions. Below it, a plain note surfaces every service where the CHEAPEST
    and FASTEST picks disagree (today they usually agree, so it will usually say
    they align). The per-category lines convey both lenses when they differ. No
    model name is hardcoded, so it reads naturally with any provider field.
    """
    esc = html.escape

    def lbl(c):
        return category_label(c, labels)

    lead_word = "fastest" if optimize == "latency" else "cheapest"
    parts = ['<div class="owner">']
    if everyday is None:
        parts.append('<p class="headline">No model clears the bar on any category '
                     'yet. Review the checks in each section below.</p>')
    else:
        everyday_cats = sorted(lbl(c) for c, i in lead_route.items()
                               if i["model"] == everyday)
        other = {}
        none_cats = []
        for c, i in lead_route.items():
            if i["model"] is None:
                none_cats.append(lbl(c))
            elif i["model"] != everyday:
                other.setdefault(i["model"], []).append(lbl(c))
        head = ('Run <b>{e}</b> for everyday work: {cats}.').format(
            e=esc(fmt_model_name(everyday)), cats=esc(", ".join(everyday_cats)))
        sentences = [head]
        for m in sorted(other):
            sentences.append('Reach for <b>{mn}</b> on {cats}.'.format(
                mn=esc(fmt_model_name(m)), cats=esc(", ".join(sorted(other[m])))))
        if none_cats:
            sentences.append('No model clears the bar yet on {cats}; '
                             'review the checks.'.format(
                                 cats=esc(", ".join(sorted(none_cats)))))
        if not other and not none_cats:
            sentences.append('It clears the bar on every categorized service.')
        parts.append('<p class="headline">' + " ".join(sentences) + "</p>")
        parts.append('<p class="lens-note">Headline follows your <b>{lw}</b> lens. '
                     'The two frontier charts and the per-service table below show '
                     'both cost and latency.</p>'.format(lw=esc(lead_word)))

    # Disagreement note: services where the cheapest and fastest clearer differ.
    disagree = []
    for c in route_cost:
        mc = route_cost[c]["model"] if isinstance(route_cost[c], dict) else route_cost[c][0]
        ml_ = route_lat[c]["model"] if isinstance(route_lat[c], dict) else route_lat[c][0]
        if mc is not None and ml_ is not None and mc != ml_:
            disagree.append((lbl(c), mc, ml_))
    routed_any = any((route_cost[c]["model"] if isinstance(route_cost[c], dict)
                      else route_cost[c][0]) is not None for c in route_cost)
    if disagree:
        frags = ["<b>{c}</b> (cheapest {a}, fastest {b})".format(
            c=esc(cl), a=esc(fmt_model_name(mc)), b=esc(fmt_model_name(ml_))) for cl, mc, ml_ in sorted(disagree)]
        parts.append('<p class="disagree-note">Cheapest and fastest picks differ '
                     'on: ' + "; ".join(frags) + ".</p>")
    elif routed_any:
        parts.append('<p class="agree-note">On every service that clears the bar, '
                     'the cheapest and the fastest model are the same.</p>')

    parts.append("<ul>")
    for c in sorted(lead_route):
        i = lead_route[c]
        mc = route_cost[c]["model"]
        ml_ = route_lat[c]["model"]
        if i["model"] is None:
            parts.append(
                '<li class="none"><span class="cat">{cat}</span> '
                '<span class="arrow">-></span> <span class="mdl">no model clears '
                'it yet</span> <span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), why=esc(i["reason"])))
        elif mc is not None and ml_ is not None and mc != ml_:
            parts.append(
                '<li><span class="cat">{cat}</span> <span class="arrow">-></span> '
                'cheapest <span class="mdl">{cm}</span>, fastest '
                '<span class="mdl">{fm}</span> '
                '<span class="why">(the two lenses disagree here)</span></li>'.format(
                    cat=esc(lbl(c)), cm=esc(fmt_model_name(mc)), fm=esc(fmt_model_name(ml_))))
        else:
            parts.append(
                '<li><span class="cat">{cat}</span> <span class="arrow">-></span> '
                'run <span class="mdl">{m}</span> '
                '<span class="why">({why})</span></li>'.format(
                    cat=esc(lbl(c)), m=esc(fmt_model_name(i["model"])), why=esc(i["reason"])))
    parts.append("</ul></div>")
    return "".join(parts)


def _dual_routing_html(cat_aggs, bar, disc_bar, labels, latency_ceiling=None):
    """Per-service dual routing table: for each category the CHEAPEST model that
    clears the bar (with its cost/test) and the FASTEST (with its median
    latency). When the two picks are the SAME model the cell collapses across
    both columns and notes "cheapest and fastest". When no model clears, the row
    says so and names the closest (strongest) model."""
    esc = html.escape
    dr = dual_routing(cat_aggs, bar, disc_bar, latency_ceiling)
    parts = ['<h2>Per-service routing</h2>',
             '<table class="routing"><thead><tr>'
             '<th class="l">service</th><th class="l">cheapest</th>'
             '<th class="l">fastest</th></tr></thead><tbody>']
    for c in sorted(dr):
        lbl = category_label(c, labels)
        cheap_m, cheap_cost, _cl = dr[c]["cheapest"]
        fast_m, _fc, fast_lat = dr[c]["fastest"]
        if cheap_m is None:
            strong = strongest_model(cat_aggs[c])
            no_floor_tests = bool(cat_aggs[c]) and all(
                s.get("floor_rate") is None for s in cat_aggs[c].values())
            if no_floor_tests:
                note = ('no floor tests in this category - by discriminating '
                        'score, {strong} ranks highest').format(
                            strong=esc(fmt_model_name(str(strong))))
                row_cls = "no-floor"
            else:
                note = 'no model clears the bar yet ({strong} is closest)'.format(
                    strong=esc(fmt_model_name(str(strong))))
                row_cls = "none"
            parts.append(
                '<tr class="{cls}"><td class="l"><b>{cat}</b></td>'
                '<td class="l" colspan="2">{note}</td></tr>'.format(
                    cls=row_cls, cat=esc(lbl), note=note))
        elif cheap_m == fast_m:
            parts.append(
                '<tr><td class="l"><b>{cat}</b></td>'
                '<td class="l" colspan="2"><span class="mdl">{m}</span> '
                '<span class="rt-note">cheapest and fastest</span> '
                '<span class="rt-metric">{cost}, {lat}</span></td></tr>'.format(
                    cat=esc(lbl), m=esc(fmt_model_name(cheap_m)),
                    cost=esc(fmt_cost_u(cheap_cost, cheap_m)), lat=esc(fmt_latency(fast_lat))))
        else:
            parts.append(
                '<tr><td class="l"><b>{cat}</b></td>'
                '<td class="l"><span class="mdl">{cm}</span> '
                '<span class="rt-metric">{cost}</span></td>'
                '<td class="l"><span class="mdl">{fm}</span> '
                '<span class="rt-metric">{lat}</span></td></tr>'.format(
                    cat=esc(lbl), cm=esc(fmt_model_name(cheap_m)), cost=esc(fmt_cost_u(cheap_cost, cheap_m)),
                    fm=esc(fmt_model_name(fast_m)), lat=esc(fmt_latency(fast_lat))))
    parts.append('</tbody></table>')
    return "".join(parts)


def _drilldown_html(owner_route, cat_aggs, cat_fails, labels, incumbent, latency_ceiling=None):
    """Per-category <details>, closed by default. Summary = the owner line;
    expansion = the per-model table for that category plus the DEDUPED floor
    failures within it."""
    esc = html.escape
    parts = ['<h2>Per-category detail</h2>']
    for c in sorted(cat_aggs):
        i = owner_route.get(c, {"model": None, "reason": "", "strongest": None})
        lbl = category_label(c, labels)
        if i["model"] is None:
            label = "NO FLOOR TESTS HERE" if i.get("no_floor_tests") else "NO MODEL CLEARS THE BAR"
            summ = ('<span class="cat">{cat}</span> <span class="arrow">-></span> '
                    '<span class="mdl">{label}</span>'
                    '<span class="why">{why}</span>').format(
                        cat=esc(lbl), label=label,
                        why=(" - " + esc(i["reason"])) if i.get("reason") else "")
            none_attr = ' data-no-floor="1"' if i.get("no_floor_tests") else ' data-none="1"'
        else:
            summ = ('<span class="cat">{cat}</span> <span class="arrow">-></span> '
                    'run <span class="mdl">{m}</span>'
                    '<span class="why">({why})</span>').format(
                        cat=esc(lbl), m=esc(fmt_model_name(i["model"])), why=esc(i["reason"]))
            none_attr = ''
        parts.append('<details class="cat"' + none_attr + '><summary>' + summ + '</summary>')
        parts.append('<div class="cat-body">')
        parts.append(_model_table(cat_aggs[c], i["model"], incumbent, latency_ceiling))
        fails = cat_fails.get(c, [])
        if fails:
            parts.append('<div class="fail-head">Floor tests failed here '
                         '(deduped across repeats)</div><ul class="fails">')
            for m, test, nf, runs in sorted(fails):
                parts.append('<li><b>{mn}</b> fails {t} ({nf}/{runs} runs)</li>'.format(
                    mn=esc(fmt_model_name(m)), t=esc(test), nf=nf, runs=runs))
            parts.append("</ul>")
        else:
            parts.append('<p class="clean">Every model cleared every floor test '
                         'in this category.</p>')
        parts.append("</div></details>")
    return "".join(parts)


def _suite_health_html(tests, layered):
    """Deduped suite-health block: floor failures counted per (model, test), and
    saturated discriminating tests (one line per test)."""
    esc = html.escape
    parts = ['<h2>Suite health</h2>']
    if not layered:
        parts.append('<p class="note">Tests were not tagged by layer, so no '
                     'saturation / regression checks were run (they need '
                     'floor / discriminating tags).</p>')
        return "".join(parts)
    saturated, regressions = suite_health(tests)
    if not saturated and not regressions:
        parts.append('<p class="clean">No issues: no saturated discriminating '
                     'tests, and every model cleared every floor test.</p>')
        return "".join(parts)
    total_models = max((t["n_models"] for t in tests.values()
                        if t["layer"] == "floor"), default=0)
    if regressions:
        import collections
        by_model = collections.OrderedDict()
        fail_models_by_test = collections.defaultdict(set)
        for m, test, nf, runs in regressions:
            by_model.setdefault(m, []).append((test, nf, runs))
            fail_models_by_test[test].add(m)

        # Tests failed by many models are almost always the TEST's fault (too
        # strict, or naked-recall), not a real per-model regression. Surface them
        # first so the reviewer fixes the suite, not the models.
        thresh = max(2, (total_models + 1) // 2) if total_models else 2
        suspect = sorted(((t, len(ms)) for t, ms in fail_models_by_test.items()
                          if len(ms) >= thresh), key=lambda x: (-x[1], x[0]))
        if suspect:
            parts.append('<div class="fail-head" style="color:#8a6d00">Floor tests '
                         'failed by many models (likely the TEST, not the models - '
                         'too strict or naked-recall; review these first)</div>'
                         '<ul class="sat">')
            for t, n in suspect:
                parts.append('<li>{t} <span style="color:#889">- fails {n}/{tot} '
                             'models</span></li>'.format(
                                 t=esc(t), n=n, tot=(total_models or "?")))
            parts.append("</ul>")

        # Per-model, collapsible (worst first). Collapsed by default so the section
        # stays short; Expand-all opens them.
        parts.append('<div class="fail-head">Floor failures by model</div>')
        for m in sorted(by_model, key=lambda k: (-len(by_model[k]), k)):
            rows = sorted(by_model[m])
            parts.append(
                '<details class="cat"><summary><span class="cat">{mn}</span> '
                '<span class="why">{n} floor test{s} failed</span></summary>'
                '<div class="cat-body"><ul class="fails">'.format(
                    mn=esc(fmt_model_name(m)), n=len(rows), s=("" if len(rows) == 1 else "s")))
            for test, nf, runs in rows:
                parts.append('<li>{t} <span style="color:#889">({nf}/{runs} runs)'
                             '</span></li>'.format(t=esc(test), nf=nf, runs=runs))
            parts.append('</ul></div></details>')
    if saturated:
        parts.append('<div class="fail-head" style="color:#8a6d00">Saturated '
                     'discriminating tests (no ranking signal; harden or retire)'
                     '</div><ul class="sat">')
        for test in saturated:
            parts.append('<li>' + esc(test) + "</li>")
        parts.append("</ul>")
    return "".join(parts)


def _run_cost_html(records, judge_id=None):
    """A run-cost-and-tokens panel: per-model generation tokens (prompt /
    completion / reasoning) and cost, a generation subtotal, the grading (judge)
    tokens with an estimated cost, and a grand total. Mirrors promptfoo's
    end-of-run token summary and adds the spend. Generation cost is promptfoo's own
    per-record figure; grading cost is estimated from the judge's grading tokens at
    its list price (see _PRICE_PER_M), since promptfoo does not price grading."""
    esc = html.escape
    import collections
    prov = collections.OrderedDict()
    g_p = g_c = 0
    for rec in records or []:
        pid = rec.get("provider")
        pid = pid.get("id") if isinstance(pid, dict) else pid
        pid = pid or "?"
        tu = (rec.get("response") or {}).get("tokenUsage") or rec.get("tokenUsage") or {}
        cd = tu.get("completionDetails") or {}
        d = prov.setdefault(pid, {"req": 0, "p": 0, "c": 0, "r": 0, "t": 0, "cost": 0.0})
        d["req"] += 1
        p_ = tu.get("prompt") or 0
        c_ = tu.get("completion") or 0
        d["p"] += p_
        d["c"] += c_
        d["r"] += cd.get("reasoning") or 0
        # Use promptfoo's own per-record total: providers disagree on whether
        # reasoning is counted inside completion (opus) or added on top (gemini),
        # and this total resolves it. Fall back to prompt+completion if absent.
        d["t"] += tu.get("total") or (p_ + c_)
        cst = rec.get("cost")
        if isinstance(cst, (int, float)):
            d["cost"] += cst
        gtu = (rec.get("gradingResult") or {}).get("tokensUsed") or {}
        g_p += gtu.get("prompt") or 0
        g_c += gtu.get("completion") or 0
    if not prov:
        return ""
    tot = {"req": 0, "p": 0, "c": 0, "r": 0, "t": 0, "cost": 0.0}
    for d in prov.values():
        for k in tot:
            tot[k] += d[k]
    gen_tokens = tot["t"]
    grade_tokens = g_p + g_c
    total_tokens = gen_tokens + grade_tokens
    jprice = None
    if judge_id:
        for k, v in _PRICE_PER_M.items():
            if k in judge_id:
                jprice = v
                break
    grade_cost = (g_p * jprice[0] / 1e6 + g_c * jprice[1] / 1e6) if jprice else None
    total_cost = tot["cost"] + (grade_cost or 0.0)
    jname = esc(judge_id.split(":")[-1]) if judge_id else "?"
    C = lambda n: "{:,}".format(int(n))
    D = lambda x: "${:,.2f}".format(x)

    out = ['<h2>Run cost and tokens</h2>']
    if grade_cost is not None:
        head = ('Total run spend <b>~{tc}</b>: generation {gc} + grading ~{grc} '
                '(judge {j}, estimated). {tt} tokens = {gen} generation + {grd} '
                'grading.').format(tc=D(total_cost), gc=D(tot["cost"]),
                                   grc=D(grade_cost), j=jname, tt=C(total_tokens),
                                   gen=C(gen_tokens), grd=C(grade_tokens))
    else:
        head = ('Generation spend <b>{gc}</b> over {gen} generation tokens; grading '
                'used {grd} tokens on the judge (not auto-priced here). {tt} tokens '
                'total.').format(gc=D(tot["cost"]), gen=C(gen_tokens),
                                 grd=C(grade_tokens), tt=C(total_tokens))
    out.append('<p class="runcost-head">' + head + '</p>')
    out.append('<table class="runcost"><thead><tr><th class="l">model</th>'
               '<th>requests</th><th>prompt</th><th>completion</th><th>reasoning</th>'
               '<th>total tokens</th><th>cost</th></tr></thead><tbody>')
    for pid, d in sorted(prov.items(),
                         key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"]))):
        out.append('<tr><td class="l">{m}</td><td>{rq}</td><td>{p}</td><td>{c}</td>'
                   '<td>{r}</td><td>{t}</td><td>{cost}</td></tr>'.format(
                       m=esc(pid), rq=C(d["req"]), p=C(d["p"]), c=C(d["c"]),
                       r=C(d["r"]), t=C(d["t"]),
                       cost=(D(d["cost"]) if d["cost"] else "$0.00 (local)")))
    out.append('<tr class="subtotal"><td class="l">generation subtotal</td>'
               '<td>{rq}</td><td>{p}</td><td>{c}</td><td>{r}</td><td>{t}</td>'
               '<td>{cost}</td></tr>'.format(rq=C(tot["req"]), p=C(tot["p"]),
                                             c=C(tot["c"]), r=C(tot["r"]),
                                             t=C(gen_tokens), cost=D(tot["cost"])))
    grc = ("~" + D(grade_cost) + " est" if grade_cost is not None else "not auto-priced")
    out.append('<tr class="grading"><td class="l">grading (judge {j})</td><td></td>'
               '<td>{p}</td><td>{c}</td><td>-</td><td>{t}</td><td>{gc}</td></tr>'.format(
                   j=jname, p=C(g_p), c=C(g_c), t=C(grade_tokens), gc=grc))
    out.append('<tr class="total"><td class="l">total</td><td></td><td></td><td></td>'
               '<td></td><td>{t}</td><td>{tc}</td></tr>'.format(
                   t=C(total_tokens),
                   tc=("~" + D(total_cost) if grade_cost is not None else D(tot["cost"]))))
    out.append('</tbody></table>')
    out.append('<p class="runcost-note">Generation cost is promptfoo\'s own '
               'per-model figure. Grading cost is a LIST-PRICE UPPER BOUND: the '
               'judge\'s grading tokens at its list rate, with no caching or volume '
               'discounts, and promptfoo does not meter grading itself. Actual billed '
               'cost is usually lower, and provider usage dashboards lag (Anthropic '
               'more than most), so confirm real spend in each provider\'s usage '
               'console. Local models are free. The grading estimate assumes ONE '
               'constant judge for the whole file (read from the results file\'s '
               'stored config) - if this file was assembled by merging multiple runs '
               'made under different judges (e.g. the judge was changed partway '
               'through a suite\'s lifetime and old + new records were combined), '
               'the estimate silently uses whichever judge the file currently '
               'reports and can misprice the other records\' grading tokens.</p>')
    return "".join(out)


def render_html(agg, layered, bar, disc_bar, rec_model_id, incumbent, tests=None,
                cat_aggs=None, categorized=False, optimize="cost", records=None,
                labels=None, judge_id=None, top_n=8, latency_ceiling=None):
    esc = html.escape
    labels = labels or {}
    cat_aggs = cat_aggs if cat_aggs is not None else {}
    # Both routing lenses are always computed. The headline leads with --optimize;
    # the frontier charts and the per-service table always show both.
    route_cost = owner_routing(cat_aggs, bar, disc_bar, "cost", latency_ceiling)
    route_lat = owner_routing(cat_aggs, bar, disc_bar, "latency", latency_ceiling)
    lead_route = route_lat if optimize == "latency" else route_cost
    everyday = everyday_pick(lead_route, optimize, agg)
    note = ("" if layered else
            '<p class="note">Tests were not tagged by layer, so floor = overall '
            'pass-rate and disc = mean score across all tests.</p>')

    if categorized and lead_route:
        owner = _owner_summary_html(lead_route, route_cost, route_lat, everyday,
                                    labels, optimize)
    else:
        pick = ("fastest by median latency" if optimize == "latency" else "cheapest")
        line = ("Run <b>{}</b>, the {} model that clears the bar.".format(
            esc(rec_model_id), pick) if rec_model_id
            else "No model clears the bar (floor pass-rate at or above "
                 "{:.0f}%).".format(bar * 100))
        owner = ('<div class="owner"><p class="headline">' + line + "</p>"
                 '<p class="why" style="color:#778">No test carried a category, so '
                 'per-category routing is absent. Tag tests with metadata.category '
                 'to enable it.</p></div>')

    # Two frontier charts, always: overall cheapest and overall fastest clearer
    # are ringed in their respective charts; both share the model color map and
    # the same zoomed y-axis floor. One shared legend serves both.
    colors = _model_colors(agg)
    ylo = _frontier_ylo(agg)
    rec_cost = recommend(agg, bar, disc_bar, "cost", latency_ceiling)
    rec_lat = recommend(agg, bar, disc_bar, "latency", latency_ceiling)
    legend = _frontier_legend_html(agg, colors, rec_cost, rec_lat, incumbent, top_n)
    svg_cost = _svg_frontier_plot(agg, colors, "cost", rec_cost, incumbent, ylo, top_n)
    svg_lat = _svg_frontier_plot(agg, colors, "latency", rec_lat, incumbent, ylo, top_n)
    frontiers = ('<div class="frontier-row">'
                 + '<div class="chart"><div class="chart-title">cost vs quality'
                   '</div>' + svg_cost + '</div>'
                 + '<div class="chart"><div class="chart-title">latency vs quality'
                   '</div>' + svg_lat + '</div>'
                 + '</div>'
                 + legend)

    overall_table = _model_table(agg, rec_cost, incumbent, latency_ceiling)
    if categorized and cat_aggs:
        dual_table = _dual_routing_html(cat_aggs, bar, disc_bar, labels, latency_ceiling)
        cat_fails = category_floor_failures(records or [])
        drilldown = _drilldown_html(lead_route, cat_aggs, cat_fails, labels, incumbent, latency_ceiling)
    else:
        dual_table = ""
        drilldown = ""
    health = _suite_health_html(tests if tests is not None else {}, layered)
    runcost = _run_cost_html(records or [], judge_id)
    db = ("" if disc_bar <= 0
          else ", disc score at or above {:.2f}".format(disc_bar))

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clawhound model recommendation</title>
<style>{css}</style></head><body>
<div class="controls">
<button onclick="cwAll(true)">Expand all</button>
<button onclick="cwAll(false)">Collapse all</button>
</div>
<div class="wrap">
<h1>Which model to run</h1>
{owner}
{note}
<h2>Cost and latency vs quality</h2>
{frontiers}
<h2>All models at a glance</h2>
<table class="legend-table"><tbody>
<tr><td class="l"><b>floor</b></td><td class="l">must-pass correctness and guardrails, the routing gate (you want 100%).</td></tr>
<tr><td class="l"><b>disc</b></td><td class="l">discriminating, the graded hard-reasoning score, 0 to 1, used to rank models.</td></tr>
<tr><td class="l"><b>latency</b></td><td class="l">median response time.</td></tr>
<tr><td class="l"><b>cost /100</b></td><td class="l">API cost per 100 tests <b>measured on this suite</b> - it reflects each model's own verbosity here, not just its list price, so it moves if your real tasks are longer (per-test is fractions of a cent; the run-cost panel shows real totals).</td></tr>
</tbody></table>
{overall_table}
{dual_table}
{drilldown}
{runcost}
{health}
<p class="foot">Bar: floor pass-rate at or above {barp:.0f}%{db}. Generated by clawhound from a promptfoo results file.</p>
</div>
<script>{script}</script>
</body></html>""".format(
        css=_REPORT_CSS, owner=owner, note=note, frontiers=frontiers,
        dual_table=dual_table, overall_table=overall_table,
        drilldown=drilldown, health=health, barp=bar * 100, db=db,
        runcost=runcost, script=_REPORT_JS)
