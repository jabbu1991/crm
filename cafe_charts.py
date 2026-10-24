"""Small server-side SVG charts for the cafe analysis page (no JavaScript library, works offline).

Colours follow the validated reference palette: series 1 blue, 2 orange, 3 aqua. Text uses text colours, never the
series colour. Every mark carries data-tip for the hover tooltip; the same values are in the tables below each chart.
"""
import math
from html import escape

from markupsafe import Markup

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"
SERIES = [BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN]
GRID, AXIS_TEXT = "#e6e8ee", "#6b7385"


def _rs(paise):
    v = paise / 100
    a = abs(v)
    if a >= 10000000:
        s = f"{v / 10000000:.1f}Cr"
    elif a >= 100000:
        s = f"{v / 100000:.1f}L"
    elif a >= 1000:
        s = f"{v / 1000:.1f}K"
    else:
        s = f"{v:.0f}"
    s = s.replace(".0", "")
    return ("-₹" + s[1:]) if s.startswith("-") else "₹" + s


def _full(paise):
    from cafe import rupees
    return rupees(paise)


def _nice_max(v):
    if v <= 0:
        return 100
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def _ticks(lo, hi, n=4):
    span = hi - lo
    step = span / n
    return [lo + step * i for i in range(n + 1)]


def line_chart(labels, series, height=240):
    """labels: x labels; series: [{'name', 'color', 'values' (paise)}]. One y axis."""
    W, Hh = 720, height
    left, right, top, bottom = 56, 16, 14, 30
    pw, ph = W - left - right, Hh - top - bottom
    n = len(labels)
    vmax = _nice_max(max([max(s["values"] or [0]) for s in series] + [0]))
    sx = (lambda i: left + (pw * i / (n - 1) if n > 1 else pw / 2))
    sy = (lambda v: top + ph - ph * v / vmax)
    out = [f'<svg class="chart" viewBox="0 0 {W} {Hh}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in _ticks(0, vmax):
        y = sy(t)
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{_rs(t)}</text>')
    step = max(1, math.ceil(n / 8))
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n - 1:
            out.append(f'<text x="{sx(i):.1f}" y="{Hh - 8}" text-anchor="middle" class="ax">{escape(lab)}</text>')
    for s in series:
        vals = s["values"]
        if not vals:
            continue
        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(vals))
        if s.get("area"):
            out.append(f'<polygon points="{sx(0):.1f},{sy(0):.1f} {pts} {sx(n - 1):.1f},{sy(0):.1f}" '
                       f'fill="{s["color"]}" opacity=".1"/>')
        out.append(f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        if n <= 40:
            for i, v in enumerate(vals):
                out.append(f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="4" fill="{s["color"]}" stroke="#fff" '
                           f'stroke-width="2"/>')
    # hover columns: one tooltip lists every series at that x
    colw = pw / max(1, n - 1) if n > 1 else pw
    for i, lab in enumerate(labels):
        tip = escape(lab) + "|" + "|".join(f'{escape(s["name"])}: {_full(s["values"][i])}' for s in series)
        x = sx(i) - colw / 2
        out.append(f'<rect class="hit" x="{max(left, x):.1f}" y="{top}" width="{colw:.1f}" height="{ph}" '
                   f'fill="transparent" data-tip="{tip}" data-x="{sx(i):.1f}"/>')
    out.append("</svg>")
    return Markup("".join(out))


def bar_chart(labels, series, height=240):
    """Grouped columns; values may be negative (profit). One y axis with a zero baseline."""
    W, Hh = 720, height
    left, right, top, bottom = 56, 16, 14, 30
    pw, ph = W - left - right, Hh - top - bottom
    allv = [v for s in series for v in s["values"]] or [0]
    vmax = _nice_max(max(max(allv), 0))
    vmin = -_nice_max(-min(allv)) if min(allv) < 0 else 0
    span = vmax - vmin
    sy = (lambda v: top + ph * (vmax - v) / span)
    out = [f'<svg class="chart" viewBox="0 0 {W} {Hh}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in _ticks(vmin, vmax):
        y = sy(t)
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{_rs(t)}</text>')
    n, k = len(labels), len(series)
    band = pw / max(1, n)
    bw = min(24, (band * 0.7 - 2 * (k - 1)) / max(1, k))
    zero = sy(0)
    for i, lab in enumerate(labels):
        cx = left + band * i + band / 2
        out.append(f'<text x="{cx:.1f}" y="{Hh - 8}" text-anchor="middle" class="ax">{escape(lab)}</text>')
        gx = cx - (bw * k + 2 * (k - 1)) / 2
        for j, s in enumerate(series):
            v = s["values"][i]
            x = gx + j * (bw + 2)
            y1, y2 = sorted((sy(v), zero))
            h = max(0.5, y2 - y1)
            r = min(4, bw / 2, h)
            # rounded data end, square at the baseline
            if v >= 0:
                d = (f"M{x:.1f},{y2:.1f} V{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} H{x + bw - r:.1f} "
                     f"Q{x + bw:.1f},{y1:.1f} {x + bw:.1f},{y1 + r:.1f} V{y2:.1f} Z")
            else:
                d = (f"M{x:.1f},{y1:.1f} V{y2 - r:.1f} Q{x:.1f},{y2:.1f} {x + r:.1f},{y2:.1f} H{x + bw - r:.1f} "
                     f"Q{x + bw:.1f},{y2:.1f} {x + bw:.1f},{y2 - r:.1f} V{y1:.1f} Z")
            tip = f'{escape(lab)}|{escape(s["name"])}: {_full(v)}'
            out.append(f'<path d="{d}" fill="{s["color"]}" data-tip="{tip}"/>')
    out.append(f'<line x1="{left}" x2="{W - right}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="#c9ced8" stroke-width="1"/>')
    out.append("</svg>")
    return Markup("".join(out))


def donut(parts, size=200):
    """parts: [(label, paise)] - at most 6 (the caller folds the rest into 'Other')."""
    total = sum(v for _, v in parts) or 1
    r, rin = size / 2 - 4, size / 2 - 34
    c = size / 2
    out = [f'<svg class="chart donut" viewBox="0 0 {size} {size}" role="img">']
    a0 = -math.pi / 2
    gap = 0.012 if len(parts) > 1 else 0
    for i, (lab, v) in enumerate(parts):
        frac = v / total
        a1 = a0 + frac * 2 * math.pi
        s, e = a0 + gap, a1 - gap
        if e <= s:
            a0 = a1
            continue
        large = 1 if e - s > math.pi else 0
        if frac >= 0.999:
            e = s + 2 * math.pi - 0.0001
            large = 1
        p = (f"M{c + r * math.cos(s):.2f},{c + r * math.sin(s):.2f} "
             f"A{r},{r} 0 {large} 1 {c + r * math.cos(e):.2f},{c + r * math.sin(e):.2f} "
             f"L{c + rin * math.cos(e):.2f},{c + rin * math.sin(e):.2f} "
             f"A{rin},{rin} 0 {large} 0 {c + rin * math.cos(s):.2f},{c + rin * math.sin(s):.2f} Z")
        tip = f"{escape(lab)}|{_full(v)} · {frac * 100:.0f}%"
        out.append(f'<path d="{p}" fill="{SERIES[i % len(SERIES)]}" data-tip="{tip}"/>')
        a0 = a1
    out.append(f'<text x="{c}" y="{c - 2}" text-anchor="middle" class="donut-total">{_rs(total)}</text>'
               f'<text x="{c}" y="{c + 16}" text-anchor="middle" class="ax">total</text></svg>')
    return Markup("".join(out))


def fold(parts, keep=5):
    parts = sorted(parts, key=lambda p: -p[1])
    if len(parts) <= keep + 1:
        return parts
    return parts[:keep] + [("Other", sum(v for _, v in parts[keep:]))]
