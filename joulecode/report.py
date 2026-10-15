"""Report as JSON (machine-readable) and Markdown (for humans)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from joulecode.html_report import generator_version, render_measurement_html
from joulecode.protocol import MeasurementResult, repetitions_text

LABELS = {"primary": "Primary metric", "wall_s": "Wall time (s)", "cpu_s": "CPU time (s)"}


def write_reports(result: MeasurementResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "result.json"
    json_path.write_text(
        json.dumps(asdict(result), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (out_dir / "report.html").write_text(
        render_measurement_html(result, version=generator_version()), encoding="utf-8"
    )
    return json_path, md_path


def _fmt(x: float | None, digits: int = 3) -> str:
    return "–" if x is None else f"{x:.{digits}g}" if abs(x) < 1000 else f"{x:,.0f}"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def render_markdown(r: MeasurementResult) -> str:
    unit = next((run["unit"] for run in r.runs if run.get("unit")), "")
    quantity = r.backend.get("quantity", "")
    lines = ["# joule:code measurement report", ""]
    lines.append(f"- Status: **{r.status}**")
    lines.append(f"- Period: {r.started_at} – {r.finished_at}")
    if r.backend:
        lines.append(f"- Back-end: `{r.backend.get('name')}` – {quantity}")
        if not r.backend.get("is_energy", True):
            lines.append("- **Note:** this back-end does not measure energy but CPU time as a proxy.")
    m = r.config.get("measurement", {})
    lines.append(
        f"- Protocol: {m.get('warmup')} warmup block(s), {repetitions_text(r)}, "
        f"{m.get('cooldown_s')} s pause, interleaved in random order"
    )
    lines.append("")

    lines.extend(f"> {v['name']}: {note}" for v in r.variants for note in v.get("notes", []))
    lines.append("")

    lines += ["## Test gate", "", "| Variant | Result | Duration |", "|---|---|---|"]
    for g in r.gates:
        lines.append(
            f"| {g.variant} | {'passed' if g.ok else f'failed (exit {g.exit_code})'} | {g.wall_s:.1f} s |"
        )
    lines.append("")
    for g in r.gates:
        if not g.ok:
            lines += [f"Output {g.variant}:", "```", g.output_tail, "```", ""]

    if r.error:
        lines += ["## Error", "", "```", r.error, "```", ""]

    if r.summary:
        lines += ["## Results (median without warmup)", ""]
        names = list(r.summary)
        lines.append("| Metric | " + " | ".join(names) + " |")
        lines.append("|---" * (len(names) + 1) + "|")
        for label in LABELS:
            row = []
            for n in names:
                s = r.summary[n].get(label)
                row.append("–" if not s else f"{_fmt(s['median'])} (±{s['cv'] * 100:.0f}%)")
            name = f"{LABELS[label]} [{unit}]" if label == "primary" else LABELS[label]
            lines.append(f"| {name} | " + " | ".join(row) + " |")
        lines.append("")
        lines.append("In brackets: coefficient of variation (spread relative to the mean).")
        lines.append("")

    if r.comparison:
        base = r.variants[0]["name"] if r.variants else "Original"
        lines += [f"## Comparison against {base}", ""]
        lines += [
            "| Variant | Metric | Change | 95% CI | p (Mann-Whitney) | Verdict |",
            "|---|---|---|---|---|---|",
        ]
        for cand, metrics in r.comparison.items():
            for label, c in metrics.items():
                lines.append(
                    f"| {cand} | {LABELS[label]} | {_pct(c['rel_change'])} | {_pct(c['ci_low'])} … {_pct(c['ci_high'])} "
                    f"| {c['p_value']:.3g} | {c['verdict']} |"
                )
        lines.append("")
        for cand, metrics in r.comparison.items():
            prim = metrics.get("primary")
            if prim:
                lines.append(f"- **{cand}:** primary metric {_pct(prim['rel_change'])} – {prim['verdict']}.")
        lines.append("")

    if r.warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in r.warnings] + [""]

    if r.runs:
        lines += ["## Individual runs", "", "| Run | Variant | Warmup | Wall time s | CPU s | Value | Note |"]
        lines.append("|---|---|---|---|---|---|---|")
        for run in r.runs:
            lines.append(
                f"| {run['run_id']} | {run['variant']} | {'yes' if run['warmup'] else ''} | {_fmt(run.get('wall_s'))} "
                f"| {_fmt(run.get('cpu_s'))} | {_fmt(run.get('value'))} | {run.get('note') or ''} |"
            )
        lines.append("")
    return "\n".join(lines)
