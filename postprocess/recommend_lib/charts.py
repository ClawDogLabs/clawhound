"""Inline SVG frontier charts (no external libraries) + the shared model color
map and legend. Two charts (cost-vs-quality, latency-vs-quality) share one
color map, one zoomed y-axis floor, and one legend, so a model is visually
identical across both."""

import html
import math

from .formatting import fmt_rate, fmt_cost_u, fmt_latency, fmt_model_name


def _frontier_ylo(agg):
    """Lower bound (in percent) for the zoomed floor-pass-rate axis.

    min(80, floor(lowest model pass-rate in percent)). So a suite whose worst
    model sits at 94% gets an 80-to-100 axis (dots spread out instead of
    crushing against the top), while a genuinely weak 71% gets a 71-to-100 axis.
    The bound NEVER starts above 80, so a strong suite always keeps 20 points of
    visible headroom. Returns 0 only in the degenerate all-unknown case.
    """
    rates = [s["floor_rate"] for s in agg.values() if s.get("floor_rate") is not None]
    if not rates:
        return 0
    return min(80, int(math.floor(min(rates) * 100)))


# Dot palette (adopted from the Broadsheet design's print-ink ramps): color a dot
# by VENDOR FAMILY and shade it by floor within the family (best = darkest ink).
# The free / local tier takes the neutral GRAY ramp, so the weakest models recede
# exactly as in the design. Greens / purples stay reserved for the ring markers.
_FAMILY_RAMPS = {
    "anthropic": ["#790e3d", "#aa0b56", "#d82071", "#ff458e", "#ff90b1"],  # magenta ink
    "cyan":      ["#004961", "#006786", "#1186ac", "#38a6cf", "#62c5ee"],  # cyan / blue (OpenAI + Google)
    "local":     ["#444141", "#605d5d", "#7d7979", "#9b9797"],             # neutral gray (free / local)
    "other":     ["#8a6d00", "#b8890f", "#c99a1a", "#edbb00"],             # process yellow (any other vendor)
}
_GROUP_GRAY = "#9b9797"   # the collapsed "+N more" group dot

_WIN_RING = "#1a7f37"   # green ring = the pick for THIS chart's metric
_INC_RING = "#8250df"   # purple dashed ring = incumbent, "you are here"


def _model_family(model, s):
    """Which ink ramp a model draws from. Local / free first (they read gray),
    then vendor by id prefix; anything else falls to the 'other' ramp."""
    mid = (model or "").lower()
    if ("ollama" in mid or "localhost" in mid or "127.0.0.1" in mid
            or "lm-studio" in mid or s.get("cost_per_test") == 0):
        return "local"
    if mid.startswith("anthropic") or "claude" in mid:
        return "anthropic"
    if (mid.startswith("openai") or "gpt" in mid or "o1" in mid or "o3" in mid
            or mid.startswith("google") or "gemini" in mid):
        return "cyan"
    return "other"


def _model_colors(agg):
    """{model: color}. Grouped by vendor family, shaded by floor within the family
    (best = darkest ink). Local / free models take the neutral gray ramp. Same map
    is used by both frontier charts and the shared legend, so a model's color is
    identical everywhere."""
    fam = {}
    for m, s in agg.items():
        fam.setdefault(_model_family(m, s), []).append((m, s))
    colors = {}
    for f, members in fam.items():
        ramp = _FAMILY_RAMPS.get(f, _FAMILY_RAMPS["other"])
        members.sort(key=lambda ms: (ms[1].get("floor_rate") is None,
                                     -(ms[1].get("floor_rate") or 0.0), ms[0]))
        for i, (m, _s) in enumerate(members):
            colors[m] = ramp[min(i, len(ramp) - 1)]
    return colors


def _frontier_rank(items):
    """Rank (model, stats) pairs by overall quality: floor desc, then disc desc,
    then id. The single ordering the charts, the group split, and the legend all
    share so dot identity follows the table's floor-descending order."""
    return sorted(items, key=lambda ms: (ms[1].get("floor_rate") is None,
                                          -(ms[1].get("floor_rate") or 0.0),
                                          -(ms[1].get("disc") or 0.0), ms[0]))


def _frontier_split(items, top_n):
    """Top-N shown individually; the rest collapse to one group whose anchor is the
    BEST remainder (so the group dot sits at a real coordinate). Only collapses when
    it would fold at least TWO models - folding a single one into '+1 more' is
    pointless, so in that case everything is shown."""
    ranked = _frontier_rank(items)
    if top_n and top_n > 0 and len(ranked) > top_n + 1:
        return ranked[:top_n], ranked[top_n:], ranked[top_n]
    return ranked, [], None


def _svg_frontier_plot(agg, colors, metric, ringed, incumbent, ylo, top_n=None):
    """One plot-only frontier SVG (no legend; the legend is shared across both
    charts and rendered once beside them).

    metric    'cost' -> x is cost per test; 'latency' -> x is median latency (s).
    ringed    the model to green-ring as the pick for THIS metric (or None).
    incumbent the model to mark with the purple dashed "you are here" ring.
    ylo       the shared zoomed y-axis floor (percent) so BOTH charts use one
              identical floor-pass-rate scale (min(80, lowest rate)..100).
    Dots are colored by the shared `colors` map (model identity), so the same
    model is the same color in both charts.
    """
    latency = (metric == "latency")
    field = "latency_s" if latency else "cost_per_test"
    axis_word = "median latency" if latency else "cost"
    pts = [(m, s) for m, s in agg.items()
           if s.get(field) is not None and s.get("floor_rate") is not None]
    W, H = 430, 330
    ml, mr, mt, mb = 56, 18, 16, 54
    pw, ph = W - ml - mr, H - mt - mb
    if not pts:
        return ('<svg width="{w}" height="80"><text x="10" y="45" '
                'font-family="system-ui" font-size="13">No models reported a {a}; '
                'nothing to plot on the {a} axis.</text></svg>').format(w=W, a=axis_word)
    # Rank by floor and split into the top_n shown individually plus a collapsed
    # gray group anchored at the best remainder. max_x fits only the DRAWN dots.
    shown, grouped, anchor = _frontier_split(pts, top_n)
    drawn = shown + ([anchor] if anchor else [])
    max_x = (max(s[field] for _, s in drawn) or 1e-9) * 1.15
    ylo_f = ylo / 100.0               # fraction
    span = (1.0 - ylo_f) or 1e-9

    def px(c):
        return ml + (c / max_x) * pw

    def py(rate):
        r = ylo_f if rate is None else rate
        r = max(ylo_f, min(1.0, r))
        return mt + (1 - (r - ylo_f) / span) * ph

    parts = ['<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
             'font-family="system-ui, sans-serif">'.format(w=W, h=H)]
    parts.append('<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>'.format(w=W, h=H))
    # axes
    parts.append('<line x1="{x}" y1="{t}" x2="{x}" y2="{b}" stroke="#888"/>'.format(
        x=ml, t=mt, b=mt + ph))
    parts.append('<line x1="{x}" y1="{b}" x2="{r}" y2="{b}" stroke="#888"/>'.format(
        x=ml, b=mt + ph, r=ml + pw))
    # y grid + labels (zoomed pass-rate ylo..100), shared across both charts
    for i in range(0, 5):
        pct = ylo + (100 - ylo) * i / 4.0
        y = py(pct / 100.0)
        parts.append('<line x1="{x}" y1="{y}" x2="{r}" y2="{y}" stroke="#eef0f4"/>'.format(
            x=ml, y=y, r=ml + pw))
        parts.append('<text x="{x}" y="{y}" font-size="11" fill="#667" '
                     'text-anchor="end">{v:.0f}%</text>'.format(x=ml - 8, y=y + 4, v=pct))
    # x labels (this chart's metric: cost per test, or median latency in seconds)
    for i in range(0, 5):
        c = max_x * i / 4.0
        x = px(c)
        lbl = "{v:.3f}s".format(v=c) if latency else "${v:,.2f}".format(v=c * 100)
        parts.append('<text x="{x}" y="{y}" font-size="10" fill="#667" '
                     'text-anchor="middle">{l}</text>'.format(x=x, y=mt + ph + 16, l=lbl))
    axis_title = ("median latency per test (s), lower is better" if latency
                  else "cost per 100 tests (USD), lower is better")
    parts.append('<text x="{x}" y="{y}" font-size="11" fill="#333" '
                 'text-anchor="middle">{t}</text>'.format(
                     x=ml + pw / 2, y=H - 8, t=html.escape(axis_title)))
    parts.append('<text transform="translate(14,{y}) rotate(-90)" font-size="11" '
                 'fill="#333" text-anchor="middle">floor pass-rate</text>'.format(
                     y=mt + ph / 2))
    def xlab(v):
        return fmt_latency(v) if latency else fmt_cost_u(v)

    # Top-N shown individually, colored by vendor ink. Incumbent gets a purple
    # dashed ring; the pick for THIS chart's metric a green ring; a model can carry
    # both. data-model drives cross-highlight with the legend and table.
    for m, s in shown:
        x, y = px(s[field]), py(s["floor_rate"])
        parts.append(
            '<g class="cw-dot" data-model="{m}" data-floor="{fr}" '
            'data-metric="{mw}" data-xlabel="{xl}">'.format(
                m=html.escape(m), fr=html.escape(fmt_rate(s["floor_rate"])),
                mw=("latency" if latency else "cost"), xl=html.escape(xlab(s[field]))))
        if incumbent and m == incumbent:
            parts.append('<circle cx="{x}" cy="{y}" r="10" fill="none" '
                         'stroke="{c}" stroke-width="2" stroke-dasharray="3 2"/>'.format(
                             x=x, y=y, c=_INC_RING))
        if m == ringed:
            parts.append('<circle cx="{x}" cy="{y}" r="12" fill="none" '
                         'stroke="{c}" stroke-width="2.5"/>'.format(x=x, y=y, c=_WIN_RING))
        parts.append('<circle class="dot" cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(
                         x=x, y=y, c=colors.get(m, "#0969da")))
        parts.append('</g>')
    # The collapsed remainder: ONE gray dot at the best remainder's real (x, y),
    # with an always-on "+N" label. Its data-model is the group key so it
    # cross-highlights with its legend entry.
    if anchor:
        am, asx = anchor
        gx, gy = px(asx[field]), py(asx["floor_rate"])
        gkey = "+{} more".format(len(grouped))
        parts.append(
            '<g class="cw-dot" data-model="{k}" data-floor="{fr}" data-metric="{mw}" '
            'data-xlabel="{xl}">'.format(
                k=html.escape(gkey), fr=html.escape(fmt_rate(asx["floor_rate"])),
                mw=("latency" if latency else "cost"), xl=html.escape(xlab(asx[field]))))
        parts.append('<circle class="dot" cx="{x}" cy="{y}" r="6" fill="{c}" '
                     'stroke="#fff" stroke-width="1.5"/>'.format(x=gx, y=gy, c=_GROUP_GRAY))
        parts.append('<text x="{x}" y="{y}" dy="-9" font-size="10" text-anchor="middle" '
                     'fill="#667">{k}</text>'.format(x=gx, y=gy, k=html.escape(gkey)))
        parts.append('</g>')
    parts.append("</svg>")
    return "".join(parts)


def _frontier_legend_html(agg, colors, rec_cost, rec_lat, incumbent, top_n=None):
    """The single shared legend for BOTH frontier charts: one row per model with
    its color swatch (same color used in both charts) and BOTH metrics, plus a
    marker key. Rendered once, not duplicated per chart."""
    esc = html.escape
    shown, grouped, _anchor = _frontier_split(list(agg.items()), top_n)
    parts = ['<div class="frontier-legend"><div class="fl-models">']
    for m, s in shown:
        tags = []
        if m == rec_cost:
            tags.append("cheapest")
        if m == rec_lat:
            tags.append("fastest")
        if incumbent and m == incumbent:
            tags.append("you are here")
        tagtxt = (' <span class="fl-tag">(' + ", ".join(tags) + ")</span>") if tags else ""
        parts.append(
            '<span class="legend-item" data-model="{m}"><span class="swatch" '
            'style="background:{c}"></span><b>{display_m}</b>{tag}</span>'.format(
                c=colors.get(m, "#0969da"), m=esc(m), display_m=esc(fmt_model_name(m)), tag=tagtxt))
    if grouped:
        gkey = "+{} more".format(len(grouped))
        parts.append(
            '<span class="legend-item" data-model="{k}"><span class="swatch" '
            'style="background:{c}"></span><b>{k} models</b></span>'.format(
                k=esc(gkey), c=_GROUP_GRAY))
    parts.append('</div></div>')
    return "".join(parts)
