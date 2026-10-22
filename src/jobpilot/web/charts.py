"""Stacked bar charts drawn as inline SVG on the server: no chart library, no CDN."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

W, H = 640, 200              # viewBox; the SVG scales to the panel width
LEFT, RIGHT, TOP, BOTTOM = 28, 6, 10, 22
GAP = 2                      # px of surface between stacked segments and between bars


@dataclass
class Segment:
    key: str
    y: float
    h: float
    top: bool                # the visible end of the bar gets the rounded corners


@dataclass
class Column:
    x: float
    w: float
    label: str               # axis label, "" when skipped to avoid crowding
    tip: str                 # hover text: the day and every series
    total: int
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Chart:
    columns: list[Column]
    ticks: list[tuple[float, int]]   # (y, value) for the grid lines
    series: list[tuple[str, str, int]]  # (key, label, total in the period) for the legend
    empty: bool
    width: int = W
    height: int = H
    left: int = LEFT
    right: int = W - RIGHT
    base: int = H - BOTTOM


def _nice_max(top: int) -> tuple[int, int]:
    """Axis top and step: 1, 2 or 5 times a power of ten, three or four steps."""
    if top <= 4:
        return max(top, 1), 1
    raw = top / 4
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    return int(math.ceil(top / step) * step), int(step)


def stacked(days: list, series: dict[str, list[int]], labels: dict[str, str]) -> Chart:
    """Days on x, one stacked bar per day; series order is fixed by the caller (color follows the key)."""
    keys = list(series)
    n = len(days)
    totals = [sum(series[k][i] for k in keys) for i in range(n)]
    top, step = _nice_max(max(totals, default=0))
    plot_h = H - TOP - BOTTOM
    slot = (W - LEFT - RIGHT) / max(n, 1)
    bar_w = max(slot - GAP, 1)
    every = max(1, math.ceil(n / 15))       # at most ~15 day labels
    scale = plot_h / top
    columns = []
    for i, day in enumerate(days):
        x = LEFT + i * slot + (slot - bar_w) / 2
        y = H - BOTTOM
        parts = [(k, series[k][i]) for k in keys if series[k][i]]
        segs = []
        for j, (k, v) in enumerate(parts):
            h = v * scale
            gap = GAP if j < len(parts) - 1 and h > GAP + 1 else 0
            y -= h
            segs.append(Segment(k, y, h - gap, j == len(parts) - 1))
        tip = day.strftime("%d.%m") + ": " + (", ".join(
            f"{labels.get(k, k)} {series[k][i]}" for k in keys) if len(keys) > 1 else str(totals[i]))
        label = day.strftime("%d.%m") if (n - 1 - i) % every == 0 else ""
        columns.append(Column(x, bar_w, label, tip, totals[i], segs))
    ticks = [(H - BOTTOM - v * scale, v) for v in range(0, top + 1, step)]
    legend = [(k, labels.get(k, k), sum(series[k])) for k in keys]
    return Chart(columns, ticks, legend, empty=not any(totals))


def bar_path(x: float, y: float, w: float, h: float, rounded: bool) -> str:
    """A bar segment; the top one gets 4 px rounded corners, the base stays square on the axis."""
    if h <= 0:
        return ""
    r = min(4.0, w / 2, h) if rounded else 0
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}"
            + (f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}"
               if r else f"H{x + w:.1f}")
            + f"V{y + h:.1f}Z")
