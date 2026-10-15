"""HTML reports for `joulecode measure` and `joulecode optimize`.

One single self-contained file per report: CSS, charts (SVG) and the little bit of
JavaScript for tooltips and the diff view are embedded in the page. Nothing is
loaded from elsewhere – the report works on the measurement machine without network,
as an e-mail attachment and in the archive. Light and dark follow the system setting.

The charts are drawn here in Python as SVG; the numbers behind them are always
shown next to them as text or a table as well.
"""

from __future__ import annotations

import difflib
import html
import math
import re
import statistics
from dataclasses import dataclass

from joulecode.protocol import MeasurementResult, repetitions_text

# -- Formatting ------------------------------------------------------------------
LABELS = {"primary": "Primary metric", "wall_s": "Wall time", "cpu_s": "CPU time"}
VERDICT = {
    "better": ("good", "✓", "better"),
    "worse": ("bad", "✗", "worse"),
    "no significant difference": ("neutral", "≈", "no significant difference"),
}
STATUS = {
    "invalid": ("bad", "not applicable"),
    "tests_failed": ("bad", "tests failed"),
    "vetoed": ("warn", "veto (maintainability)"),
    "ready": ("neutral", "admitted, not measured"),
    "measured": ("info", "measured"),
    "measurement_failed": ("bad", "measurement failed"),
    "duplicate": ("neutral", "duplicate"),
    "new": ("neutral", "new"),
}


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def num(x: float | None, digits: int = 3) -> str:
    """Number with `digits` significant digits, English format (1,234.5)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    if x == 0:
        return "0"
    if abs(x) >= 10**digits:
        s = f"{x:,.0f}"
    else:
        decimals = max(0, digits - 1 - int(math.floor(math.log10(abs(x)))))
        s = f"{x:,.{decimals}f}"
    return s.replace("-", "−")


def pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "–"
    if round(x * 100, digits) == 0:
        return "0%"
    return f"{x * 100:+.{digits}f}%".replace("-", "−")


def score(x) -> str:
    """Maintainability score 1–5 without superfluous decimals."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return esc(x)
    return str(int(v)) if v.is_integer() else num(v, 2)


def short(text: str, n: int = 38) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# -- Page skeleton --------------------------------------------------------------
CSS = """
:root {
  color-scheme: light;
  --bg: #f4f4f1; --surface: #fcfcfb; --surface-2: #f0efec; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --muted: #7d7c77; --grid: #e6e5e0; --axis: #b9b8b2;
  --good: #0ca30c; --good-text: #006300; --good-bg: #e4f4e2;
  --bad: #d03b3b; --bad-text: #a82525; --bad-bg: #fbe7e6;
  --warn: #ec835a; --warn-text: #9a4115; --warn-bg: #fcece4;
  --neutral: #8f8e88; --neutral-bg: #ecebe7;
  --info: #2a78d6; --info-text: #1c5cab; --info-bg: #e3eefb;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a;
  --del-bg: #fdecec; --del-mark: #f6c3c3; --add-bg: #e6f5e4; --add-mark: #b5e2b0;
  --code-bg: #f7f7f5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #2f2f2c;
    --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86; --grid: #2a2a27; --axis: #55544f;
    --good: #0ca30c; --good-text: #6fd46f; --good-bg: #16301a;
    --bad: #e05252; --bad-text: #ff8a8a; --bad-bg: #3a1c1c;
    --warn: #ec835a; --warn-text: #f4a888; --warn-bg: #3a251a;
    --neutral: #8f8e86; --neutral-bg: #2a2a27;
    --info: #3987e5; --info-text: #8dbcf2; --info-bg: #182a40;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70;
    --del-bg: #351b1b; --del-mark: #6a2b2b; --add-bg: #15301a; --add-mark: #23582b;
    --code-bg: #161615;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #2f2f2c;
  --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86; --grid: #2a2a27; --axis: #55544f;
  --good: #0ca30c; --good-text: #6fd46f; --good-bg: #16301a;
  --bad: #e05252; --bad-text: #ff8a8a; --bad-bg: #3a1c1c;
  --warn: #ec835a; --warn-text: #f4a888; --warn-bg: #3a251a;
  --neutral: #8f8e86; --neutral-bg: #2a2a27;
  --info: #3987e5; --info-text: #8dbcf2; --info-bg: #182a40;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70;
  --del-bg: #351b1b; --del-mark: #6a2b2b; --add-bg: #15301a; --add-mark: #23582b;
  --code-bg: #161615;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums;
}
main { max-width: 1080px; margin: 0 auto; padding: 28px 24px 64px; }
@media (max-width: 640px) { main { padding: 20px 16px 48px; } }
a { color: var(--info-text); }
code, pre, .mono { font-family: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, "Liberation Mono", monospace; font-size: 13px; }
h1 { font-size: 26px; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 0 0 4px; }
h3 { font-size: 15px; margin: 18px 0 6px; }
p { margin: 6px 0; }
.lede { color: var(--text-2); margin: 0 0 14px; max-width: 72ch; }
.brand { display: flex; align-items: center; gap: 8px; color: var(--text-2); font-size: 13px; font-weight: 600;
  letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 8px; }
.brand i { width: 10px; height: 10px; border-radius: 50%; background: var(--good); display: inline-block; }
header.page { margin-bottom: 22px; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--text-2); font-size: 13.5px; margin-top: 10px; }
.meta b { color: var(--text); font-weight: 600; }
section.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px 22px; margin: 16px 0; }
@media (max-width: 640px) { section.card { padding: 16px 14px; } }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 16px 0; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.kpi .label { color: var(--text-2); font-size: 13px; }
.kpi .value { font-size: 28px; font-weight: 650; line-height: 1.2; margin-top: 2px; letter-spacing: -0.01em; }
.kpi .sub { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
.pill { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border-radius: 999px;
  font-size: 12.5px; font-weight: 600; white-space: nowrap; }
.pill.good { background: var(--good-bg); color: var(--good-text); }
.pill.bad { background: var(--bad-bg); color: var(--bad-text); }
.pill.warn { background: var(--warn-bg); color: var(--warn-text); }
.pill.neutral { background: var(--neutral-bg); color: var(--text-2); }
.pill.info { background: var(--info-bg); color: var(--info-text); }
.callout { border-left: 3px solid var(--warn); background: var(--warn-bg); padding: 8px 12px; border-radius: 0 8px 8px 0;
  margin: 8px 0; color: var(--text); font-size: 14px; }
.callout.bad { border-color: var(--bad); background: var(--bad-bg); }
.callout.info { border-color: var(--info); background: var(--info-bg); }
.table-wrap { overflow-x: auto; margin: 8px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--text-2); font-weight: 600; font-size: 12.5px; white-space: nowrap; }
td.num, th.num { text-align: right; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
.funnel { display: flex; flex-wrap: wrap; align-items: stretch; gap: 6px; margin: 10px 0 2px; }
.funnel .step { background: var(--surface-2); border-radius: 8px; padding: 6px 12px; min-width: 92px; }
.funnel .step b { display: block; font-size: 20px; line-height: 1.2; }
.funnel .step span { color: var(--text-2); font-size: 12.5px; }
.funnel .arrow { color: var(--muted); align-self: center; }
.chart-wrap { overflow-x: auto; margin: 4px 0; }
svg.chart { width: 100%; min-width: 700px; height: auto; display: block; overflow: visible; }
svg.chart text { fill: var(--text-2); font-size: 12px; font-family: inherit; }
svg.chart .t-strong { fill: var(--text); font-weight: 600; }
svg.chart .t-muted { fill: var(--muted); }
svg.chart .grid { stroke: var(--grid); stroke-width: 1; }
svg.chart .axis { stroke: var(--axis); stroke-width: 1; }
svg.chart .zero { stroke: var(--text-2); stroke-width: 1.5; stroke-dasharray: 4 3; }
svg.chart .band { fill: transparent; }
svg.chart .band:hover { fill: var(--surface-2); }
svg.chart .good { stroke: var(--good); fill: var(--good); }
svg.chart .bad { stroke: var(--bad); fill: var(--bad); }
svg.chart .neutral { stroke: var(--neutral); fill: var(--neutral); }
svg.chart .ring { stroke: var(--surface); stroke-width: 2; }
svg.chart .hollow { fill: var(--surface); stroke-width: 2; }
svg.chart .s1 { stroke: var(--s1); fill: var(--s1); }
svg.chart .s2 { stroke: var(--s2); fill: var(--s2); }
svg.chart .s3 { stroke: var(--s3); fill: var(--s3); }
svg.chart .warmup { fill: var(--surface); stroke: var(--muted); stroke-width: 1.5; }
svg.chart .median { stroke: var(--text); stroke-width: 2; }
svg.chart .discarded { stroke: var(--bad); stroke-width: 2; fill: none; stroke-linecap: round; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; color: var(--text-2); font-size: 13px; margin: 6px 0 0; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
.legend i.ring { background: var(--surface); border: 2px solid var(--text-2); }
.legend i.s1 { background: var(--s1); } .legend i.s2 { background: var(--s2); } .legend i.s3 { background: var(--s3); }
.legend i.good { background: var(--good); } .legend i.bad { background: var(--bad); } .legend i.neutral { background: var(--neutral); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
details.cand { border: 1px solid var(--border); border-radius: 10px; margin: 10px 0; background: var(--surface); }
details.cand > summary { list-style: none; cursor: pointer; padding: 12px 14px; display: flex; flex-wrap: wrap;
  align-items: center; gap: 6px 12px; }
details.cand > summary::-webkit-details-marker { display: none; }
details.cand > summary::before { content: "▸"; color: var(--muted); transition: transform .15s; }
details.cand[open] > summary::before { transform: rotate(90deg); }
details.cand > summary .title { font-weight: 600; flex: 1 1 260px; min-width: 0; }
details.cand > summary .id { color: var(--muted); font-weight: 600; margin-right: 6px; }
details.cand > summary .delta { font-weight: 650; white-space: nowrap; }
.cand-body { padding: 0 16px 16px; border-top: 1px solid var(--border); }
.facts { display: grid; grid-template-columns: minmax(9em, max-content) minmax(0, 1fr); gap: 3px 14px; font-size: 14px; margin: 10px 0; }
@media (max-width: 640px) { .facts { grid-template-columns: minmax(0, 1fr); gap: 0; } .facts dd { margin-bottom: 6px; } }
.facts dd, .facts code { overflow-wrap: anywhere; }
.facts dt { color: var(--text-2); }
.facts dd { margin: 0; }
ul.plain { margin: 4px 0; padding-left: 20px; }
pre.out { background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  overflow-x: auto; max-height: 340px; margin: 6px 0; white-space: pre; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 6px 0 4px; color: var(--text-2); font-size: 13px; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { font: inherit; font-size: 13px; border: 0; background: var(--surface); color: var(--text-2); padding: 4px 10px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--surface-2); color: var(--text); font-weight: 600; }
.diff { border: 1px solid var(--border); border-radius: 8px; overflow-x: auto; background: var(--code-bg); margin: 8px 0; }
.diff .file { padding: 6px 10px; border-bottom: 1px solid var(--border); color: var(--text-2); font-size: 12.5px; background: var(--surface-2); }
.diff table { font-size: 12.5px; width: 100%; border-collapse: collapse; table-layout: auto; }
.diff td { border: 0; padding: 0 8px; white-space: pre; font-family: ui-monospace, "SF Mono", Consolas, monospace;
  line-height: 1.6; vertical-align: top; }
.diff td.ln { color: var(--muted); text-align: right; user-select: none; width: 1%; padding: 0 6px; }
.diff td.sign { color: var(--muted); width: 1%; padding: 0 2px; user-select: none; }
.diff tr.hunk td { color: var(--muted); background: var(--surface-2); padding: 2px 8px; }
.diff td.del { background: var(--del-bg); } .diff td.add { background: var(--add-bg); }
.diff td.del mark { background: var(--del-mark); color: inherit; border-radius: 2px; }
.diff td.add mark { background: var(--add-mark); color: inherit; border-radius: 2px; }
.diff td.empty { background: var(--surface-2); }
.diff table.split-view { table-layout: fixed; min-width: 640px; }
.diff col.c-ln { width: 3.4em; } .diff col.c-sign { width: 1.5em; }
.diff table.split-view td.code { white-space: pre-wrap; overflow-wrap: anywhere; }
.diff .split-view td.split + td.ln { border-left: 1px solid var(--border); }
html[data-diff="unified"] .split-view { display: none; }
html:not([data-diff="unified"]) .unified-view { display: none; }
#tip { position: fixed; z-index: 10; pointer-events: none; background: var(--text); color: var(--surface);
  padding: 6px 9px; border-radius: 6px; font-size: 12.5px; line-height: 1.45; white-space: pre-line; max-width: 320px;
  box-shadow: 0 4px 14px rgba(0,0,0,.18); }
.muted { color: var(--muted); }
.small { font-size: 13px; }
nav.toc { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13.5px; margin-top: 12px; }
nav.toc a { color: var(--text-2); text-decoration: none; border-bottom: 1px solid var(--border); }
nav.toc a:hover { color: var(--text); }
footer { color: var(--muted); font-size: 12.5px; margin-top: 28px; }
@media print {
  body { background: #fff; } section.card, .kpi, details.cand { break-inside: avoid; }
  details.cand > summary::before { display: none; } .toolbar { display: none; }
}
"""

JS = """
(function () {
  var tip = document.getElementById('tip');
  function hide() { tip.hidden = true; }
  document.addEventListener('pointerover', function (e) {
    var t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) { hide(); return; }
    tip.textContent = t.getAttribute('data-tip'); tip.hidden = false;
  });
  document.addEventListener('pointermove', function (e) {
    if (tip.hidden) return;
    var w = tip.offsetWidth, h = tip.offsetHeight, x = e.clientX + 14, y = e.clientY + 14;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - 14;
    tip.style.left = Math.max(8, x) + 'px'; tip.style.top = Math.max(8, y) + 'px';
  });
  document.addEventListener('scroll', hide, true);
  var root = document.documentElement;
  function setDiff(mode) {
    root.setAttribute('data-diff', mode);
    document.querySelectorAll('[data-diff-mode]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-diff-mode') === mode ? 'true' : 'false');
    });
  }
  setDiff(window.matchMedia('(max-width: 720px)').matches ? 'unified' : 'split');
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-diff-mode]');
    if (b) setDiff(b.getAttribute('data-diff-mode'));
    var o = e.target.closest && e.target.closest('[data-open]');
    if (o) {
      var d = document.getElementById(o.getAttribute('data-open'));
      if (d) { d.open = true; }
    }
  });
})();
"""


def page(title: str, body: str, description: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n"
        + (f'<meta name="description" content="{esc(description)}">\n' if description else "")
        + f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n"
        f'<div id="tip" role="tooltip" hidden></div>\n<script>{JS}</script>\n</body>\n</html>\n'
    )


def pill(kind: str, text: str, icon: str = "") -> str:
    return f'<span class="pill {kind}">{esc(icon) + " " if icon else ""}{esc(text)}</span>'


def verdict_pill(verdict: str) -> str:
    kind, icon, text = VERDICT.get(verdict, ("neutral", "", verdict))
    return pill(kind, text, icon)


def status_pill(status: str) -> str:
    kind, text = STATUS.get(status, ("neutral", status))
    return pill(kind, text)


def kpi(label: str, value: str, sub: str = "", kind: str = "") -> str:
    style = f' style="color: var(--{kind}-text)"' if kind else ""
    return (
        f'<div class="kpi"><div class="label">{esc(label)}</div><div class="value"{style}>{value}</div>'
        + (f'<div class="sub">{sub}</div>' if sub else "")
        + "</div>"
    )


def warnings_html(warnings: list[str]) -> str:
    return "".join(f'<div class="callout">{esc(w)}</div>' for w in dict.fromkeys(warnings))


# -- Scales ---------------------------------------------------------------------
def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step - 1e-9) * step
    ticks, t = [], start
    while t <= hi + 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


@dataclass
class Scale:
    d0: float
    d1: float
    r0: float
    r1: float

    def __call__(self, v: float) -> float:
        if self.d1 == self.d0:
            return (self.r0 + self.r1) / 2
        return self.r0 + (v - self.d0) / (self.d1 - self.d0) * (self.r1 - self.r0)


def _pct_domain(values: list[float], pad: float = 0.04) -> tuple[float, float]:
    lo, hi = min([*values, 0.0]), max([*values, 0.0])
    span = max(hi - lo, 0.04)
    lo, hi = lo - span * pad, hi + span * pad
    ticks = nice_ticks(lo, hi, 5)
    return min(lo, ticks[0]), max(hi, ticks[-1])


# -- Chart: change with confidence interval (forest plot) -----------------------
@dataclass
class Effect:
    label: str
    rel: float
    lo: float
    hi: float
    verdict: str
    tip: str
    group: str = ""
    star: bool = False
    target: str = ""  # id of a <details> element that a click opens


CLIP_HI = 1.0  # +100 %


def forest_chart(effects: list[Effect], *, title: str = "") -> str:
    """Relative change per variant with 95% CI; left of zero = less."""
    if not effects:
        return ""
    width, label_w, value_w, row_h, top = 760, 230, 96, 30, 26
    groups = list(dict.fromkeys(e.group for e in effects))
    show_groups = len(groups) > 1 or groups[0]
    rows: list[tuple[str, Effect | str]] = []
    for g in groups:
        if show_groups:
            rows.append(("group", g))
        rows += [("effect", e) for e in effects if e.group == g]
    height = top + len(rows) * row_h + 34
    # Very wide intervals (e.g. +6000% after measurement dropouts) would squash everything else:
    # the axis goes up to CLIP_HI at most; beyond that the line ends in an arrow.
    lo, hi = _pct_domain([min(v, CLIP_HI) for e in effects for v in (e.lo, e.hi, e.rel)])
    x = Scale(lo, hi, label_w, width - value_w)
    parts = [
        f'<div class="chart-wrap"><svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">'
    ]
    # grid and axis
    for t in nice_ticks(lo, hi, 6):
        px = x(t)
        parts.append(f'<line class="grid" x1="{px:.1f}" y1="{top - 6}" x2="{px:.1f}" y2="{height - 28}"/>')
        parts.append(f'<text x="{px:.1f}" y="{height - 10}" text-anchor="middle">{esc(pct(t, 0))}</text>')
    zx = x(0)
    parts.append(f'<line class="zero" x1="{zx:.1f}" y1="{top - 10}" x2="{zx:.1f}" y2="{height - 28}"/>')
    parts.append(f'<text x="{zx - 6:.1f}" y="{top - 12}" text-anchor="end" class="t-muted">← less</text>')
    parts.append(f'<text x="{zx + 6:.1f}" y="{top - 12}" class="t-muted">more →</text>')
    y = top
    for kind, item in rows:
        cy = y + row_h / 2
        if kind == "group":
            parts.append(f'<text x="0" y="{cy + 4:.1f}" class="t-strong mono">{esc(short(item, 40))}</text>')
        else:
            e: Effect = item
            cls = VERDICT.get(e.verdict, ("neutral",))[0]
            attrs = f' data-tip="{esc(e.tip)}"' + (
                f' data-open="{esc(e.target)}" style="cursor:pointer"' if e.target else ""
            )
            parts.append(f"<g{attrs}>")
            parts.append(f'<rect class="band" x="0" y="{y}" width="{width}" height="{row_h}" rx="6"/>')
            indent = 12 if show_groups else 0
            label = ("★ " if e.star else "") + short(e.label, 30)
            parts.append(
                f'<text x="{indent}" y="{cy + 4:.1f}" class="{"t-strong" if e.star else ""}">{esc(label)}</text>'
            )
            x0, x1 = sorted((x(min(e.lo, hi)), x(min(e.hi, hi))))
            parts.append(
                f'<line class="{cls}" x1="{x0:.1f}" y1="{cy:.1f}" x2="{max(x1, x0 + 0.5):.1f}" y2="{cy:.1f}" '
                'stroke-width="2.5" stroke-linecap="round"/>'
            )
            if e.hi > hi:  # clipped: arrow at the right edge
                parts.append(
                    f'<path class="{cls}" d="M{x1 + 1:.1f},{cy - 5:.1f} L{x1 + 9:.1f},{cy:.1f} L{x1 + 1:.1f},{cy + 5:.1f} Z"/>'
                )
            parts.append(
                f'<circle class="{cls} ring" cx="{x(e.rel):.1f}" cy="{cy:.1f}" r="6" stroke-width="2"/>'
            )
            parts.append(
                f'<text x="{width}" y="{cy + 4:.1f}" text-anchor="end" class="t-strong">{esc(pct(e.rel))}</text>'
            )
            parts.append("</g>")
        y += row_h
    parts.append("</svg></div>")
    legend = (
        '<div class="legend"><span><i class="good"></i>✓ significantly better</span>'
        '<span><i class="neutral"></i>≈ no significant difference</span>'
        '<span><i class="bad"></i>✗ significantly worse</span>'
        "<span>Dot: median change · line: 95% confidence interval</span></div>"
    )
    return "".join(parts) + legend


# -- Chart: individual measurements per variant (strip chart) --------------------
def strip_chart(
    result: MeasurementResult, key: str = "value", unit: str = "", titles: dict | None = None
) -> str:
    """Every measurement run as a dot; vertical bar = median (without warmup). Axis starts at 0."""
    names = [v["name"] for v in result.variants] or list(dict.fromkeys(r["variant"] for r in result.runs))

    def val(r: dict) -> float | None:
        # Show discarded runs (measurement dropout) with their original value, but mark them.
        return r[key] if r.get(key) is not None else (r.get("value_raw") if key == "value" else None)

    runs = [r for r in result.runs if val(r) is not None]
    if not runs:
        return ""
    any_discarded = any(r.get("discarded") for r in runs)
    width, label_w, value_w, row_h, top = 760, 190, 110, 44, 8
    height = top + len(names) * row_h + 34
    values = [val(r) for r in runs]
    # Start at 0: the distance to the dot is then the consumption, so ratios are visible.
    lo, hi = min(0.0, min(values)), max(values) * 1.04 or 1.0
    ticks = nice_ticks(lo, hi, 5)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    x = Scale(lo, hi, label_w, width - value_w)
    parts = [
        f'<div class="chart-wrap"><svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Individual measurements">'
    ]
    for t in ticks:
        px = x(t)
        parts.append(f'<line class="grid" x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{height - 28}"/>')
        parts.append(f'<text x="{px:.1f}" y="{height - 10}" text-anchor="middle">{esc(num(t))}</text>')
    if unit:
        parts.append(
            f'<text x="{width - value_w:.1f}" y="{height - 10}" text-anchor="start" dx="8" class="t-muted">{esc(unit)}</text>'
        )
    for i, name in enumerate(names):
        y0 = top + i * row_h
        cy = y0 + row_h / 2
        parts.append(
            f'<text x="0" y="{cy + 4:.1f}" class="t-strong">{esc(short((titles or {}).get(name, name), 26))}</text>'
        )
        parts.append(
            f'<line class="grid" x1="{label_w}" y1="{cy:.1f}" x2="{width - value_w}" y2="{cy:.1f}"/>'
        )
        mine = [r for r in runs if r["variant"] == name]
        scored = [r[key] for r in mine if not r.get("warmup") and r.get(key) is not None]
        for j, r in enumerate(mine):
            # deterministic jitter in y so that equal values do not overlap
            jitter = ((j * 0.618034) % 1 - 0.5) * 20
            cls = "warmup" if r.get("warmup") else "s1 ring"
            state = " (warmup, not counted)" if r.get("warmup") else ""
            if r.get("discarded"):
                cls, state = "discarded", " – discarded (measurement dropout)"
            tip = (
                f"{name} · run {r.get('run_id')}{state}\n{num(val(r))} {unit}".rstrip()
                + (f"\nCPU time {num(r.get('cpu_s'))} s" if r.get("discarded") else "")
                + (
                    f"\nWall time {num(r.get('wall_s'))} s"
                    if key != "wall_s" and r.get("wall_s") is not None
                    else ""
                )
            )
            px, py = x(val(r)), cy + jitter
            if cls == "discarded":
                parts.append(
                    f'<g data-tip="{esc(tip)}"><rect class="band" x="{px - 7:.1f}" y="{py - 7:.1f}" width="14" height="14"/>'
                    f'<path class="discarded" d="M{px - 4.5:.1f},{py - 4.5:.1f} L{px + 4.5:.1f},{py + 4.5:.1f} '
                    f'M{px - 4.5:.1f},{py + 4.5:.1f} L{px + 4.5:.1f},{py - 4.5:.1f}"/></g>'
                )
                continue
            parts.append(
                f'<circle class="{cls}" cx="{px:.1f}" cy="{py:.1f}" r="4.5" stroke-width="1.5" '
                f'data-tip="{esc(tip)}"/>'
            )
        if scored:
            med = statistics.median(scored)
            mx = x(med)
            parts.append(
                f'<line class="median" x1="{mx:.1f}" y1="{cy - 15:.1f}" x2="{mx:.1f}" y2="{cy + 15:.1f}"/>'
            )
            parts.append(
                f'<text x="{width}" y="{cy + 4:.1f}" text-anchor="end" class="t-strong">{esc(num(med))} {esc(unit)}</text>'
            )
    parts.append("</svg></div>")
    legend = (
        '<div class="legend"><span><i class="s1"></i>counted run</span>'
        '<span><i class="ring"></i>warmup (not counted)</span>'
        + (
            '<span><b style="color:var(--bad)">×</b> discarded (measurement dropout)</span>'
            if any_discarded
            else ""
        )
        + "<span>│ Median</span></div>"
    )
    return "".join(parts) + legend


# -- Chart: energy versus maintainability (Pareto) -------------------------------
def pareto_chart(ranking: list[dict]) -> str:
    rows = [r for r in ranking if r["id"] != "combined"]
    if not rows:
        return ""
    hotspots = list(dict.fromkeys(r["hotspot"] for r in rows))
    slot = {h: f"s{min(i, 2) + 1}" for i, h in enumerate(hotspots)}  # at most three colours
    width, height, left, right, top, bottom = 760, 250, 46, 20, 30, 40
    lo, hi = _pct_domain([r["rel_change"] for r in rows], pad=0.12)
    x = Scale(lo, hi, left, width - right)
    y = Scale(1, 5, height - bottom, top)
    parts = [
        f'<div class="chart-wrap"><svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Energy versus maintainability">'
    ]
    for t in nice_ticks(lo, hi, 6):
        px = x(t)
        parts.append(f'<line class="grid" x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{height - bottom}"/>')
        parts.append(
            f'<text x="{px:.1f}" y="{height - bottom + 18}" text-anchor="middle">{esc(pct(t, 0))}</text>'
        )
    for v in range(1, 6):
        py = y(v)
        parts.append(f'<line class="grid" x1="{left}" y1="{py:.1f}" x2="{width - right}" y2="{py:.1f}"/>')
        parts.append(f'<text x="{left - 10}" y="{py + 4:.1f}" text-anchor="end">{v}</text>')
    parts.append(f'<line class="zero" x1="{x(0):.1f}" y1="{top}" x2="{x(0):.1f}" y2="{height - bottom}"/>')
    parts.append(
        f'<text x="{(left + width - right) / 2:.1f}" y="{height - 4}" text-anchor="middle" class="t-muted">Energy relative to the original (left = more efficient)</text>'
    )
    parts.append(f'<text x="0" y="{top - 16}" class="t-muted">Maintainability (1–5, reviewer)</text>')
    for r in sorted(rows, key=lambda r: r["pareto"]):
        px, py = x(r["rel_change"]), y(float(r["maintainability"]))
        cls = slot[r["hotspot"]]
        shape = f"{cls} ring" if r["pareto"] else f"{cls} hollow"
        tip = (
            f"{r['id']}: {r['title']}\nHotspot {r['hotspot']}\nEnergy {pct(r['rel_change'])} "
            f"({pct(r['ci_low'])} … {pct(r['ci_high'])})\nMaintainability {score(r['maintainability'])} of 5"
            + ("\n★ Pareto front" if r["pareto"] else "")
        )
        parts.append(f'<g data-tip="{esc(tip)}" data-open="cand-{esc(r["id"])}" style="cursor:pointer">')
        parts.append(f'<circle class="band" cx="{px:.1f}" cy="{py:.1f}" r="14"/>')
        parts.append(f'<circle class="{shape}" cx="{px:.1f}" cy="{py:.1f}" r="7" stroke-width="2"/>')
        parts.append(
            f'<text x="{px + 11:.1f}" y="{py - 9:.1f}" class="t-strong">{esc(r["id"])}{" ★" if r["pareto"] else ""}</text>'
        )
        parts.append("</g>")
    parts.append("</svg></div>")
    legend = '<div class="legend">'
    if len(hotspots) > 1:
        legend += "".join(
            f'<span><i class="{slot[h]}"></i><code>{esc(short(h, 40))}</code></span>' for h in hotspots[:3]
        )
        if len(hotspots) > 3:
            legend += f'<span><i class="s3"></i>others ({len(hotspots) - 3})</span>'
    legend += "<span>filled: Pareto front (★) · ring: dominated or not significant</span></div>"
    return "".join(parts) + legend


# -- Chart: hotspots (share of self time) ----------------------------------------
def hotspot_bars(hotspots: list[dict]) -> str:
    if not hotspots:
        return ""
    width, label_w, value_w, row_h = 760, 260, 60, 28
    height = len(hotspots) * row_h + 4
    x = Scale(0, 1, label_w, width - value_w)  # share of the total self time
    parts = [
        f'<div class="chart-wrap"><svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Hotspots">'
    ]
    for i, h in enumerate(hotspots):
        y = i * row_h + 4
        tip = f"{h['qualname']} ({h['file']}:{h['line']})\n{h['share']:.0%} of self time" + (
            f" · {h['ncalls']} calls" if h.get("ncalls") else ""
        )
        parts.append(
            f'<g data-tip="{esc(tip)}"><rect class="band" x="0" y="{y - 2}" width="{width}" height="{row_h}" rx="6"/>'
        )
        parts.append(f'<text x="0" y="{y + 15}" class="t-strong mono">{esc(short(h["qualname"], 34))}</text>')
        bw = max(2.0, x(h["share"]) - label_w)
        parts.append(f'<rect class="s1" x="{label_w}" y="{y + 4}" width="{bw:.1f}" height="14" rx="3"/>')
        parts.append(
            f'<text x="{width}" y="{y + 15}" text-anchor="end" class="t-strong">{esc(pct(h["share"], 0).lstrip("+"))}</text></g>'
        )
    parts.append("</svg></div>")
    return "".join(parts)


# -- Before/after from a unified diff -------------------------------------------
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch(patch: str) -> list[dict]:
    """Unified-Diff → [{file, hunks: [{header, lines: [(op, old_no, new_no, text)]}]}]."""
    files: list[dict] = []
    cur = hunk = None
    old_no = new_no = 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            name = line[4:].split("\t")[0]
            cur = {"file": name[2:] if name.startswith("b/") else name, "hunks": []}
            files.append(cur)
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = {"header": line, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None or line.startswith("\\"):
            continue
        op, text = (line[:1] or " "), line[1:]
        if op == "-":
            hunk["lines"].append(("-", old_no, None, text))
            old_no += 1
        elif op == "+":
            hunk["lines"].append(("+", None, new_no, text))
            new_no += 1
        else:
            hunk["lines"].append((" ", old_no, new_no, text))
            old_no += 1
            new_no += 1
    return files


_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def _intraline(a: str, b: str) -> tuple[str, str]:
    """Highlight changed words within a pair of lines."""
    ta, tb = _TOKEN.findall(a), _TOKEN.findall(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if sm.ratio() < 0.5:  # hardly anything in common: the whole line is different
        return esc(a), esc(b)
    oa, ob = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        sa, sb = esc("".join(ta[i1:i2])), esc("".join(tb[j1:j2]))
        if tag == "equal":
            oa.append(sa)
            ob.append(sb)
        else:
            if sa:
                oa.append(f"<mark>{sa}</mark>")
            if sb:
                ob.append(f"<mark>{sb}</mark>")
    return "".join(oa), "".join(ob)


def _pair_blocks(lines: list[tuple]) -> list[tuple]:
    """Combine consecutive -/+ blocks into pairs of lines."""
    rows, i = [], 0
    while i < len(lines):
        op = lines[i][0]
        if op == " ":
            rows.append(("ctx", lines[i], lines[i]))
            i += 1
            continue
        dels, adds = [], []
        while i < len(lines) and lines[i][0] == "-":
            dels.append(lines[i])
            i += 1
        while i < len(lines) and lines[i][0] == "+":
            adds.append(lines[i])
            i += 1
        rows.extend(
            ("chg", dels[k] if k < len(dels) else None, adds[k] if k < len(adds) else None)
            for k in range(max(len(dels), len(adds)))
        )
    return rows


def diff_html(patch: str) -> str:
    if not patch.strip():
        return ""
    out = []
    for f in parse_patch(patch):
        split, unified = [], []
        for h in f["hunks"]:
            split.append(f'<tr class="hunk"><td colspan="6">{esc(h["header"])}</td></tr>')
            unified.append(f'<tr class="hunk"><td colspan="4">{esc(h["header"])}</td></tr>')
            for kind, a, b in _pair_blocks(h["lines"]):
                if kind == "ctx":
                    t = esc(a[3])
                    split.append(
                        f'<tr><td class="ln">{a[1]}</td><td class="sign"></td><td class="code split">{t}</td>'
                        f'<td class="ln">{a[2]}</td><td class="sign"></td><td class="code split">{t}</td></tr>'
                    )
                    unified.append(
                        f'<tr><td class="ln">{a[1]}</td><td class="ln">{a[2]}</td><td class="sign"></td><td class="code">{t}</td></tr>'
                    )
                    continue
                ta, tb = (
                    _intraline(a[3], b[3]) if a and b else (esc(a[3]) if a else "", esc(b[3]) if b else "")
                )
                left = (
                    f'<td class="ln del">{a[1]}</td><td class="sign del">−</td><td class="code split del">{ta}</td>'
                    if a
                    else '<td class="ln empty"></td><td class="sign empty"></td><td class="code split empty"></td>'
                )
                right = (
                    f'<td class="ln add">{b[2]}</td><td class="sign add">+</td><td class="code split add">{tb}</td>'
                    if b
                    else '<td class="ln empty"></td><td class="sign empty"></td><td class="code split empty"></td>'
                )
                split.append(f"<tr>{left}{right}</tr>")
            for op, o, n, text in h["lines"]:
                cls = {"-": "del", "+": "add"}.get(op, "")
                sign = {"-": "−", "+": "+"}.get(op, "")
                unified.append(
                    f'<tr><td class="ln {cls}">{o or ""}</td><td class="ln {cls}">{n or ""}</td>'
                    f'<td class="sign {cls}">{sign}</td><td class="code {cls}">{esc(text)}</td></tr>'
                )
        out.append(
            f'<div class="diff"><div class="file mono">{esc(f["file"])}</div>'
            f'<table class="split-view"><colgroup><col class="c-ln"><col class="c-sign"><col class="c-code">'
            f'<col class="c-ln"><col class="c-sign"><col class="c-code"></colgroup><tbody>{"".join(split)}</tbody></table>'
            f'<table class="unified-view"><tbody>{"".join(unified)}</tbody></table></div>'
        )
    return "".join(out)


DIFF_TOOLBAR = (
    '<div class="toolbar">View: <span class="seg">'
    '<button type="button" data-diff-mode="split" aria-pressed="true">side by side</button>'
    '<button type="button" data-diff-mode="unified" aria-pressed="false">unified</button></span></div>'
)


# -- Building blocks from the measurement result ---------------------------------
def _unit(r: MeasurementResult) -> str:
    return next((run["unit"] for run in r.runs if run.get("unit")), "")


def measurement_effects(
    r: MeasurementResult, label: str = "primary", titles: dict | None = None
) -> list[Effect]:
    effects = []
    for cand, metrics in r.comparison.items():
        c = metrics.get(label)
        if not c:
            continue
        name = (titles or {}).get(cand, cand)
        tip = (
            f"{name}\n{LABELS[label]}: {pct(c['rel_change'])}\n95% CI {pct(c['ci_low'])} … {pct(c['ci_high'])}\n"
            f"p = {num(c['p_value'], 2)} · {c['verdict']}"
        )
        effects.append(Effect(name, c["rel_change"], c["ci_low"], c["ci_high"], c["verdict"], tip))
    return effects


def gate_table(r: MeasurementResult) -> str:
    if not r.gates:
        return ""
    rows = "".join(
        f"<tr><td>{esc(g.variant)}</td><td>{pill('good', 'passed', '✓') if g.ok else pill('bad', f'failed (exit {g.exit_code})', '✗')}</td>"
        f'<td class="num">{num(g.wall_s, 2)} s</td></tr>'
        for g in r.gates
    )
    fails = "".join(
        f'<h3>Output {esc(g.variant)}</h3><pre class="out">{esc(g.output_tail)}</pre>'
        for g in r.gates
        if not g.ok
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Variant</th><th>Tests</th><th class="num">Duration</th></tr>'
        f"</thead><tbody>{rows}</tbody></table></div>{fails}"
    )


def summary_table(r: MeasurementResult, titles: dict | None = None) -> str:
    if not r.summary:
        return ""
    unit = _unit(r)
    names = list(r.summary)
    head = "".join(f'<th class="num">{esc((titles or {}).get(n, n))}</th>' for n in names)
    body = ""
    for label in LABELS:
        cells = ""
        for n in names:
            s = r.summary[n].get(label)
            cells += (
                '<td class="num">–</td>'
                if not s
                else (
                    f'<td class="num">{num(s["median"])} <span class="muted small">±{s["cv"] * 100:.0f} %</span></td>'
                )
            )
        name = f"{LABELS[label]} [{unit}]" if label == "primary" else f"{LABELS[label]} [s]"
        body += f"<tr><td>{esc(name)}</td>{cells}</tr>"
    return (
        f'<div class="table-wrap"><table><thead><tr><th>Median (without warmup)</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
        '<p class="muted small">± coefficient of variation: spread relative to the mean. Above 10% the measurement is noisy.</p>'
    )


def comparison_table(r: MeasurementResult) -> str:
    rows = ""
    for cand, metrics in r.comparison.items():
        for label, c in metrics.items():
            rows += (
                f"<tr><td>{esc(cand)}</td><td>{esc(LABELS.get(label, label))}</td>"
                f'<td class="num"><b>{pct(c["rel_change"])}</b></td>'
                f'<td class="num">{pct(c["ci_low"])} … {pct(c["ci_high"])}</td>'
                f'<td class="num">{num(c["p_value"], 2)}</td><td>{verdict_pill(c["verdict"])}</td></tr>'
            )
    if not rows:
        return ""
    return (
        '<div class="table-wrap"><table><thead><tr><th>Variant</th><th>Metric</th><th class="num">Change</th>'
        '<th class="num">95% CI</th><th class="num">p (Mann-Whitney)</th><th>Verdict</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def runs_table(r: MeasurementResult) -> str:
    if not r.runs:
        return ""
    unit = _unit(r)
    rows = "".join(
        f"<tr><td>{esc(run['run_id'])}</td><td>{esc(run['variant'])}</td><td>{'Warmup' if run.get('warmup') else ''}</td>"
        f'<td class="num">{num(run.get("wall_s"))}</td><td class="num">{num(run.get("cpu_s"))}</td>'
        f'<td class="num">{num(run.get("value"))}</td><td class="small muted">{esc(run.get("note") or "")}</td></tr>'
        for run in r.runs
    )
    return (
        f"<details><summary>All {len(r.runs)} individual runs as a table</summary>"
        '<div class="table-wrap"><table><thead><tr><th>Run</th><th>Variant</th><th></th>'
        f'<th class="num">Wall time s</th><th class="num">CPU s</th><th class="num">Value {esc(unit)}</th><th>Note</th>'
        f"</tr></thead><tbody>{rows}</tbody></table></div></details>"
    )


def protocol_line(r: MeasurementResult) -> str:
    m = r.config.get("measurement", {})
    return (
        f"{m.get('warmup')} warmup block(s), {esc(repetitions_text(r))}, {num(m.get('cooldown_s'), 2)} s pause, "
        "each repetition with all variants in random order. Verdict “better” only if p &lt; α "
        "and the whole 95% confidence interval lies below zero."
    )


def backend_meta(r: MeasurementResult) -> str:
    b = r.backend or {}
    if not b:
        return ""
    return f"<span>Measurement: <b>{esc(b.get('name'))}</b> – {esc(b.get('quantity', ''))}</span>"


def backend_note(r: MeasurementResult) -> str:
    if r.backend and not r.backend.get("is_energy", True):
        return (
            '<div class="callout info">This back-end does not measure energy but CPU time as a proxy. '
            "For energy values, measure on the measurement machine with metrion.</div>"
        )
    return ""


# -- Measurement report (joulecode measure) --------------------------------------
MEASURE_TITLES = {"baseline": "Original", "candidate": "Candidate", "baseline-b": "Original (A/A copy)"}


def render_measurement_html(r: MeasurementResult, *, version: str = "") -> str:
    unit = _unit(r)
    effects = measurement_effects(r, titles=MEASURE_TITLES)
    is_aa = any("A/A" in n for v in r.variants for n in v.get("notes", []))
    body = ['<header class="page"><div class="brand"><i></i>joule:code</div>']
    body.append(f"<h1>{'A/A noise test' if is_aa else 'Measurement report'}</h1>")
    body.append(
        '<div class="meta">'
        f"<span>Status: {pill('good' if r.status == 'ok' else 'bad', r.status)}</span>"
        f"<span>Period: <b>{esc(_time(r.started_at))}</b> – {esc(_time(r.finished_at))}</span>"
        f"{backend_meta(r)}</div></header>"
    )
    if r.error:
        body.append(f'<div class="callout bad"><b>Error</b><pre class="out">{esc(r.error)}</pre></div>')
    body.append(backend_note(r))

    if effects:
        tiles = []
        for e in effects:
            kind = VERDICT.get(e.verdict, ("neutral",))[0]
            color = {"good": "good", "bad": "bad"}.get(kind, "")
            tiles.append(
                kpi(
                    f"{e.label} vs. original",
                    pct(e.rel),
                    f"{verdict_pill(e.verdict)} · CI {pct(e.lo)} … {pct(e.hi)}",
                    color,
                )
            )
        base = r.summary.get(r.variants[0]["name"], {}).get("primary") if r.variants else None
        if base:
            tiles.append(
                kpi(
                    "Original, median per run",
                    f'{num(base["median"])} <span class="small">{esc(unit)}</span>',
                    f"Spread ±{num(base['cv'] * 100, 2)}%",
                )
            )
        body.append(f'<div class="kpis">{"".join(tiles)}</div>')
        if is_aa:
            body.append(
                '<div class="callout info">A/A test: both variants are identical. The width of the confidence interval '
                "shows which differences this machine can resolve at all. “better” or “worse” would be a false alarm here.</div>"
            )

    if effects:
        body.append('<section class="card"><h2>Change relative to the original</h2>')
        body.append(f'<p class="lede">{protocol_line(r)}</p>')
        body.append(forest_chart(effects, title="Change of the primary metric"))
        body.append(comparison_table(r))
        body.append("</section>")
    if r.runs:
        body.append('<section class="card"><h2>Individual measurements</h2>')
        body.append(
            '<p class="lede">Every dot is a run in a fresh container. If the clouds of dots are far apart, the difference is clear; if they overlap, the statistics decide.</p>'
        )
        body.append(strip_chart(r, "value", unit, MEASURE_TITLES))
        body.append(summary_table(r, MEASURE_TITLES))
        body.append(runs_table(r))
        body.append("</section>")
    if r.warnings:
        body.append(f'<section class="card"><h2>Warnings</h2>{warnings_html(r.warnings)}</section>')
    body.append(
        f'<section class="card"><h2>Test-Gate</h2><p class="lede">Only variants that pass the tests of the original are measured.</p>{gate_table(r)}</section>'
    )
    body.append(_footer(version, ["report.md", "result.json"]))
    return page("joule:code measurement report", "\n".join(body), "Energy measurement original vs. candidate")


def _time(iso: str | None) -> str:
    if not iso:
        return "–"
    s = str(iso).replace("T", " ")
    return s[:19]


def generator_version() -> str:
    try:
        from joulecode.cli import version_string

        return version_string()
    except Exception:  # noqa: BLE001 – only for the footer
        return "joule:code"


def _footer(version: str, files: list[str]) -> str:
    links = " · ".join(f'<a href="{esc(f)}">{esc(f)}</a>' for f in files)
    return f"<footer>Generated by {esc(version or 'joule:code')} · {links}</footer>"


# -- Optimisation report (joulecode optimize) ------------------------------------
def _candidate_card(c, result, row: dict | None, measurement: MeasurementResult | None) -> str:
    rel = row["rel_change"] if row else None
    verdict = row["verdict"] if row else None
    kind = VERDICT.get(verdict, ("neutral",))[0] if verdict else ""
    delta_style = {"good": "var(--good-text)", "bad": "var(--bad-text)"}.get(kind, "var(--text-2)")
    summary = (
        f'<summary><span class="title"><span class="id">{esc(c.id)}</span>{esc(c.title)}'
        f"{' ★' if row and row.get('pareto') else ''}</span>"
        f'<code class="muted">{esc(short(c.hotspot, 44))}</code>{status_pill(c.status)}'
        + (
            f'<span class="delta" style="color:{delta_style}">{esc(pct(rel))}</span>'
            if rel is not None
            else ""
        )
        + "</summary>"
    )
    parts = ['<div class="cand-body">']
    facts = [
        ("File", f"<code>{esc(c.file)}</code>"),
        ("Changed functions", ", ".join(f"<code>{esc(n)}</code>" for n in c.changed) or "–"),
        (
            "Origin",
            esc(
                {
                    "initial": "first proposal",
                    "repair": "after repair",
                    "revision": "revision after veto",
                }.get(c.origin, c.origin)
            )
            + f", round {c.round}",
        ),
    ]
    if row and measurement is not None:
        prim = measurement.comparison.get(c.id, {}).get("primary")
        wall = measurement.comparison.get(c.id, {}).get("wall_s")
        if prim:
            facts.append(
                (
                    esc(measurement.backend.get("quantity") or "primary metric"),
                    f"<b>{pct(prim['rel_change'])}</b> (95% CI {pct(prim['ci_low'])} … {pct(prim['ci_high'])}, "
                    f"p = {num(prim['p_value'], 2)}) {verdict_pill(prim['verdict'])}",
                )
            )
        if wall:
            facts.append(("Wall time", pct(wall["rel_change"])))
    if c.review and c.review.get("score") is not None:
        facts.append(("Maintainability", f"{score(c.review.get('score'))} of 5"))
    if c.parts:
        facts.append(
            (
                "Components",
                ", ".join(
                    f'<a href="#cand-{esc(p)}" data-open="cand-{esc(p)}">{esc(p)}</a>' for p in c.parts
                ),
            )
        )
    parts.append('<dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>")
    if c.rationale:
        parts.append(f"<h3>Rationale</h3><p>{esc(c.rationale)}</p>")

    rv = c.review or {}
    met = rv.get("metrics")
    if met:
        rows = "".join(
            f'<tr><td><code>{esc(f["name"])}</code></td><td class="num">{esc(f["complexity_before"])} → {esc(f["complexity_after"])}</td>'
            f"<td>{pill('bad', 'signature changed') if f.get('signature_changed') else ''}"
            f"{pill('neutral', 'docs added') if f.get('docstring_after') and not f.get('docstring_before') else ''}</td></tr>"
            for f in met.get("functions", [])
        )
        parts.append(
            "<h3>Maintainability</h3>"
            '<div class="table-wrap"><table><thead><tr><th>Function</th><th class="num">Complexity before → after</th><th></th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            f'<p class="small">Maintainability index of the file: {esc(met.get("mi_before"))} → {esc(met.get("mi_after"))} · '
            f"{esc(met.get('diff_lines'))} changed lines · new imports: {esc(', '.join(met.get('new_imports', [])) or '–')}</p>"
        )
        notes = [f'<div class="callout bad">Rule veto: {esc(v)}</div>' for v in rv.get("rule_vetoes", [])]
        llm = rv.get("llm") or {}
        notes += [f'<div class="callout">Reviewer concerns: {esc(x)}</div>' for x in llm.get("concerns", [])]
        if llm.get("suggestion"):
            notes.append(f'<div class="callout info">Suggestion: {esc(llm["suggestion"])}</div>')
        parts += notes
    if c.docs:
        parts.append(
            '<h3>Documentation</h3><ul class="plain">'
            + "".join(f"<li>{esc(d)}</li>" for d in c.docs)
            + "</ul>"
        )
    if c.messages:
        parts.append(
            '<h3>History</h3><ul class="plain">' + "".join(f"<li>{esc(m)}</li>" for m in c.messages) + "</ul>"
        )
    if c.status == "tests_failed" and c.tests_output:
        parts.append(f'<h3>Test output</h3><pre class="out">{esc(c.tests_output)}</pre>')
    if c.patch:
        parts.append("<h3>Before / after</h3>" + DIFF_TOOLBAR + diff_html(c.patch))
    parts.append("</div>")
    return f'<details class="cand" id="cand-{esc(c.id)}">{summary}{"".join(parts)}</details>'


def render_optimize_html(result, *, version: str = "") -> str:
    """Complete report of `joulecode optimize` as one HTML page."""
    m: MeasurementResult | None = result.measurement
    cm: MeasurementResult | None = result.combination_measurement
    cands = list(result.candidates)
    everyone = cands + ([result.combination] if result.combination else [])
    by_id = {c.id: c for c in everyone}
    rows = {r["id"]: r for r in result.ranking}

    gate_ok = [
        c for c in cands if c.status in ("ready", "measured", "vetoed", "measurement_failed", "duplicate")
    ]
    measured = [c for c in cands if c.status == "measured"]
    better = [r for r in result.ranking if r["verdict"] == "better"]
    best = min((r for r in better if r["id"] != "combined"), key=lambda r: r["rel_change"], default=None)

    body = ['<header class="page"><div class="brand"><i></i>joule:code</div><h1>Optimisation report</h1>']
    body.append(
        '<div class="meta">'
        f"<span>Status: {pill('good' if result.status == 'ok' else 'bad', result.status)}</span>"
        f"<span>Period: <b>{esc(_time(result.started_at))}</b> – {esc(_time(result.finished_at))}</span>"
        f"<span>LLM: <b>{esc(result.llm or '–')}</b></span>" + (backend_meta(m) if m else "") + "</div>"
    )
    toc = [
        ("result", "Result"),
        ("candidates", "Candidates"),
        ("measurement", "Measurement"),
        ("analysis", "Hotspots & analysis"),
    ]
    body.append('<nav class="toc">' + "".join(f'<a href="#{a}">{t}</a>' for a, t in toc) + "</nav></header>")
    if result.error:
        body.append(
            f'<div class="callout bad"><b>Error:</b> <pre class="out">{esc(result.error)}</pre></div>'
        )
    if m:
        body.append(backend_note(m))

    # Key figures
    tiles = []
    if best:
        tiles.append(
            kpi(
                "Best single candidate",
                pct(best["rel_change"]),
                f"{esc(best['id'])}: {esc(short(best['title'], 34))}",
                "good",
            )
        )
    elif m:
        tiles.append(kpi("Best single candidate", "–", "no candidate significantly better"))
    combo = rows.get("combined")
    if combo:
        kind = "good" if combo["verdict"] == "better" else ""
        tiles.append(kpi("Combination", pct(combo["rel_change"]), verdict_pill(combo["verdict"]), kind))
    tiles.append(
        kpi("Candidates", str(len(cands)), f"{len(gate_ok)} with green tests · {len(measured)} measured")
    )
    hs = (result.profile or {}).get("hotspots", [])
    tiles.append(kpi("Hotspots", str(len(hs)), esc(", ".join(short(h["qualname"], 22) for h in hs)) or "–"))
    body.append(f'<div class="kpis">{"".join(tiles)}</div>')

    # Result
    body.append('<section class="card" id="result"><h2>Result</h2>')
    if result.ranking:
        body.append(
            '<p class="lede">For each hotspot, ★ marks the Pareto front: candidates that consume significantly less and '
            "for which no other candidate of the same hotspot is both more efficient and at least as maintainable. "
            "Candidates for different hotspots complement each other – their combination is shown below. A human makes the choice. "
            "Clicking a row opens the candidate.</p>"
        )
        effects = []
        for r in result.ranking:
            if r["id"] == "combined":
                continue
            tip = (
                f"{r['id']}: {r['title']}\nEnergy {pct(r['rel_change'])} (95% CI {pct(r['ci_low'])} … {pct(r['ci_high'])})\n"
                f"{r['verdict']} · maintainability {score(r['maintainability'])} of 5"
                + ("\n★ Pareto front" if r["pareto"] else "")
            )
            effects.append(
                Effect(
                    f"{r['id']} · {r['title']}",
                    r["rel_change"],
                    r["ci_low"],
                    r["ci_high"],
                    r["verdict"],
                    tip,
                    r["hotspot"],
                    r["pareto"],
                    f"cand-{r['id']}",
                )
            )
        if combo:
            tip = f"Combination\nEnergy {pct(combo['rel_change'])} (95% CI {pct(combo['ci_low'])} … {pct(combo['ci_high'])})\n{combo['verdict']}"
            effects.append(
                Effect(
                    "combined · " + by_id["combined"].title if "combined" in by_id else "combined",
                    combo["rel_change"],
                    combo["ci_low"],
                    combo["ci_high"],
                    combo["verdict"],
                    tip,
                    "Combination",
                    combo["pareto"],
                    "cand-combined",
                )
            )
        body.append(forest_chart(effects, title="Energy change per candidate"))
        table = "".join(
            f"<tr><td>{'★' if r['pareto'] else ''}</td><td><code>{esc(short(r['hotspot'], 40))}</code></td>"
            f'<td><a href="#cand-{esc(r["id"])}" data-open="cand-{esc(r["id"])}">{esc(r["id"])}</a> {esc(r["title"])}</td>'
            f'<td class="num"><b>{pct(r["rel_change"])}</b></td><td class="num">{pct(r["ci_low"])} … {pct(r["ci_high"])}</td>'
            f'<td>{verdict_pill(r["verdict"])}</td><td class="num">{score(r["maintainability"])}</td></tr>'
            for r in sorted(result.ranking, key=lambda r: r["id"] == "combined")
        )
        body.append(
            '<div class="table-wrap"><table><thead><tr><th></th><th>Hotspot</th><th>Candidate</th><th class="num">Energy</th>'
            '<th class="num">95% CI</th><th>Verdict</th><th class="num">Maintainability (1–5)</th></tr></thead>'
            f"<tbody>{table}</tbody></table></div>"
        )
        if any(r["id"] != "combined" for r in result.ranking):
            body.append("<h3>Energy versus maintainability</h3>" + pareto_chart(result.ranking))
    else:
        body.append(f'<p class="lede">{_no_result_text(result)}</p>')
    body.append("</section>")

    # Funnel + candidates
    funnel = [
        (len(cands), "proposed"),
        (len([c for c in cands if c.status != "invalid"]), "applicable"),
        (len(gate_ok), "tests green"),
        (len([c for c in gate_ok if c.status not in ("vetoed", "duplicate")]), "no veto"),
        (len(measured), "measured"),
        (len([r for r in better if r["id"] != "combined"]), "significantly better"),
    ]
    steps = '<span class="arrow">→</span>'.join(
        f'<div class="step"><b>{n}</b><span>{esc(t)}</span></div>' for n, t in funnel
    )
    body.append('<section class="card" id="candidates"><h2>Candidates</h2>')
    body.append(f'<div class="funnel">{steps}</div>')
    body.append(
        '<p class="muted small">Every candidate must pass the tests of the original (otherwise one repair attempt), gets documentation comments (docstring or PHPDoc) and is reviewed for maintainability before it is measured.</p>'
    )
    order = sorted(
        everyone,
        key=lambda c: (
            c.id == "combined",
            c.status != "measured",
            rows.get(c.id, {}).get("rel_change", 9),
            c.id,
        ),
    )
    for c in order:
        meas = cm if result.combination and c.id == result.combination.id else m
        body.append(_candidate_card(c, result, rows.get(c.id), meas))
    body.append("</section>")

    # Measurement
    if m and m.runs:
        body.append('<section class="card" id="measurement"><h2>Measurement</h2>')
        body.append(f'<p class="lede">{protocol_line(m)}</p>')
        titles = {c.id: f"{c.id} · {c.title}" for c in cands}
        body.append(
            "<h3>Individual measurements</h3>"
            + strip_chart(m, "value", _unit(m), {**titles, "baseline": "Original"})
        )
        body.append(summary_table(m, {"baseline": "Original"}))
        if cm and cm.runs:
            body.append("<h3>Second measurement: combination</h3>")
            body.append(
                '<p class="muted small">Original, best single candidate and combination in a campaign of their own.</p>'
            )
            body.append(
                forest_chart(
                    measurement_effects(cm, titles={**titles, "combined": "combined · combination"}),
                    title="Combination",
                )
            )
            body.append(
                strip_chart(
                    cm,
                    "value",
                    _unit(cm),
                    {**titles, "baseline": "Original", "combined": "combined · combination"},
                )
            )
        body.append(runs_table(m))
        body.append(
            '<p class="small"><a href="measurement/report.html">Detailed measurement report</a>'
            + (
                ' · <a href="measurement-combined/report.html">Measurement report combination</a>'
                if cm
                else ""
            )
            + "</p>"
        )
        body.append("</section>")

    warns = list(result.warnings) + (list(m.warnings) if m else []) + (list(cm.warnings) if cm else [])
    if warns:
        body.append(f'<section class="card"><h2>Warnings</h2>{warnings_html(warns)}</section>')

    # Analysis
    body.append('<section class="card" id="analysis"><h2>Hotspots &amp; static analysis</h2>')
    if hs:
        total = (result.profile or {}).get("total_s")
        body.append(
            '<p class="lede">Functions with the largest share of self time in the profiling run ('
            + esc((result.profile or {}).get("profiler", "cProfile"))
            + ")"
            + (f", total duration {num(total)} s" if total else "")
            + ". Only these are edited by the optimiser.</p>"
        )
        body.append(hotspot_bars(hs))
    elif result.profile and result.profile.get("error"):
        body.append(f'<pre class="out">{esc(result.profile["error"])}</pre>')
    met = (result.static or {}).get("metrics")
    if met:
        items = [
            ("Files", met.get("files")),
            ("Lines of code (SLOC)", met.get("sloc")),
            ("Functions", met.get("functions")),
            ("Complexity avg / max", f"{num(met.get('complexity_avg'), 2)} / {met.get('complexity_max')}"),
            ("Functions with CC > 10", met.get("complex_functions")),
            ("Maintainability index avg / min", f"{num(met.get('mi_avg'), 3)} / {num(met.get('mi_min'), 3)}"),
            (
                f"{met.get('lint_tool', 'Ruff')} findings",
                met.get("lint_findings", met.get("ruff_findings")),
            ),
            (f"Dead code ({met.get('dead_code_tool', 'vulture')})", met.get("dead_code")),
        ]
        body.append(
            '<h3>Key figures of the original</h3><dl class="facts">'
            + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in items)
            + "</dl>"
        )
        body.append('<p class="small"><a href="analysis.md">Full analysis (analysis.md)</a></p>')
    body.append("</section>")
    body.append(_footer(version, ["report.md", "result.json"]))
    return page(
        "joule:code optimisation report",
        "\n".join(body),
        "Candidates, energy measurement and before/after comparison",
    )


def _no_result_text(result) -> str:
    return {
        "baseline_tests_failed": "The original's tests fail – nothing is optimised without green tests.",
        "no_hotspots": "Profiling found no function above the threshold optimize.min_share. The runtime is "
        "probably spent in libraries, built-in functions or waiting.",
        "no_candidates": "No candidate passed tests and review – there was nothing to measure.",
        "measurement_failed": "The measurement failed; see the warnings and the measurement report for details.",
    }.get(result.status, "No measurement results.")
