"""Reports for `joulecode optimize` – overall report and one optimisation report per candidate."""

from __future__ import annotations

import json
from pathlib import Path

from joulecode.analysis import RepoMetrics, StaticAnalysis, render_analysis_markdown
from joulecode.html_report import generator_version, render_optimize_html
from joulecode.optimize import OptimizeResult
from joulecode.protocol import repetitions_text
from joulecode.report import write_reports

STATUS = {
    "invalid": "not applicable",
    "tests_failed": "tests failed",
    "vetoed": "veto (maintainability)",
    "ready": "admitted, not measured",
    "measured": "measured",
    "measurement_failed": "measurement failed",
    "duplicate": "duplicate (not measured)",
}


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _measurement_row(result: OptimizeResult, cid: str) -> dict | None:
    return next((r for r in result.ranking if r["id"] == cid), None)


def _measurement_for(result: OptimizeResult, cid: str):
    if result.combination and cid == result.combination.id:
        return result.combination_measurement
    return result.measurement


def render_candidate_report(c, result: OptimizeResult) -> str:
    """Optimisation report: what, why, measured savings, measurement method, maintainability."""
    lines = [f"# Optimisation {c.id}: {c.title}", ""]
    lines += [
        f"- File: `{c.file}`",
        f"- Hotspot: `{c.hotspot}`",
        f"- Changed functions: {', '.join(f'`{n}`' for n in c.changed)}",
        f"- Status: {STATUS.get(c.status, c.status)}",
        f"- Origin: {c.origin}, round {c.round}",
        "",
        "## Rationale",
        "",
        c.rationale or "–",
        "",
    ]
    row = _measurement_row(result, c.id)
    m = _measurement_for(result, c.id)
    if row and m:
        prim = m.comparison[c.id]["primary"]
        wall = m.comparison[c.id].get("wall_s")
        backend = m.backend
        lines += ["## Measured effect", ""]
        lines.append(
            f"- {backend.get('quantity', 'primary metric')}: **{_pct(prim['rel_change'])}** "
            f"(95% CI {_pct(prim['ci_low'])} … {_pct(prim['ci_high'])}, p = {prim['p_value']:.3g}) – {prim['verdict']}"
        )
        if wall:
            lines.append(f"- Wall time: {_pct(wall['rel_change'])}")
        mc = m.config.get("measurement", {})
        lines.append(
            f"- Method: back-end `{backend.get('name')}`, {repetitions_text(m)}, "
            f"{mc.get('warmup')} warmup, interleaved; median comparison with bootstrap CI and Mann-Whitney U"
        )
        if not backend.get("is_energy", True):
            lines.append("- Note: CPU time was measured as a proxy, not energy.")
        lines.append("")

    if c.parts:
        lines += ["## Components", ""] + [f"- [{p}]({p}.md)" for p in c.parts] + [""]
    if c.review and c.review.get("metrics"):
        rv = c.review
        met = rv["metrics"]
        lines += ["## Maintainability", "", f"- Score: {rv.get('score')} of 5"]
        lines.append(f"- Maintainability index of the file: {met['mi_before']} → {met['mi_after']}")
        for f in met["functions"]:
            lines.append(
                f"- `{f['name']}`: complexity {f['complexity_before']} → {f['complexity_after']}"
                + (", **signature changed**" if f["signature_changed"] else "")
            )
        lines.append(
            f"- Diff: {met['diff_lines']} changed lines; new imports: {', '.join(met['new_imports']) or '–'}"
        )
        if rv.get("rule_vetoes"):
            lines += [f"- Rule veto: {v}" for v in rv["rule_vetoes"]]
        llm = rv.get("llm")
        if llm:
            lines += [f"- Concern: {x}" for x in llm.get("concerns", [])]
            if llm.get("suggestion"):
                lines.append(f"- Suggestion: {llm['suggestion']}")
        lines.append("")

    if c.docs:
        lines += ["## Documentation", ""] + [f"- {d}" for d in c.docs] + [""]
    if c.messages:
        lines += ["## History", ""] + [f"- {x}" for x in c.messages] + [""]
    if c.status == "tests_failed" and c.tests_output:
        lines += ["## Test output", "", "```", c.tests_output, "```", ""]
    if c.patch:
        lines += ["## Change", "", "```diff", c.patch.rstrip(), "```", ""]
    return "\n".join(lines)


def render_optimize_report(result: OptimizeResult) -> str:
    lines = ["# joule:code optimisation report", ""]
    lines += [f"- Status: **{result.status}**", f"- Period: {result.started_at} – {result.finished_at}"]
    lines += [f"- LLM: `{result.llm}`", ""]
    if result.error:
        lines += ["## Error", "", "```", result.error, "```", ""]

    if result.ranking:
        lines += [
            "## Result",
            "",
            "For each hotspot, the Pareto front (★) contains the candidates that consume significantly less and "
            "for which no other candidate of the same hotspot is both more efficient and at least as maintainable. "
            "Candidates for different hotspots complement each other; their combination is listed separately below. "
            "A human makes the choice.",
            "",
            "| | Hotspot | Candidate | Energy | 95% CI | Verdict | Maintainability (1–5) |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in result.ranking:
            lines.append(
                f"| {'★' if r['pareto'] else ''} | `{r['hotspot']}` | [{r['id']}](candidates/{r['id']}.md): {r['title']} "
                f"| {_pct(r['rel_change'])} | {_pct(r['ci_low'])} … {_pct(r['ci_high'])} | {r['verdict']} "
                f"| {r['maintainability']} |"
            )
        lines += ["", "Measurement details: [measurement/report.md](measurement/report.md)", ""]

    combo = result.combination
    if combo:
        lines += [
            "## Combination",
            "",
            f"[{combo.id}](candidates/{combo.id}.md): {combo.title} – Status: {STATUS.get(combo.status, combo.status)}",
        ]
        cm = result.combination_measurement
        if cm and cm.status == "ok":
            lines.append("")
            for cand, metrics in cm.comparison.items():
                prim = metrics.get("primary")
                if prim:
                    lines.append(
                        f"- {cand} vs. original: {_pct(prim['rel_change'])} "
                        f"({_pct(prim['ci_low'])} … {_pct(prim['ci_high'])}) – {prim['verdict']}"
                    )
            lines += [
                "",
                "Second measurement with the original, the best single candidate and the combination: "
                "[measurement-combined/report.md](measurement-combined/report.md)",
            ]
        lines.append("")

    if result.candidates:
        lines += ["## All candidates", "", "| Candidate | Hotspot | Status | Note |", "|---|---|---|---|"]
        for c in result.candidates:
            note = c.messages[-1] if c.messages else ""
            lines.append(
                f"| [{c.id}](candidates/{c.id}.md): {c.title} | `{c.hotspot}` | {STATUS.get(c.status, c.status)} | {note} |"
            )
        lines.append("")

    if result.warnings or (result.measurement and result.measurement.warnings):
        lines += ["## Warnings", ""]
        lines += [f"- {w}" for w in result.warnings]
        if result.measurement:
            lines += [f"- {w}" for w in result.measurement.warnings]
        lines.append("")
    lines += ["Analysis of the original: [analysis.md](analysis.md)", ""]
    return "\n".join(lines)


def _static_from_dict(d: dict) -> StaticAnalysis:
    from joulecode.analysis import FileMetrics, Finding, FunctionMetrics

    return StaticAnalysis(
        metrics=RepoMetrics(**d["metrics"]),
        functions=[FunctionMetrics(**f) for f in d["functions"]],
        files=[FileMetrics(**f) for f in d["files"]],
        findings=[Finding(**f) for f in d["findings"]],
        coupling=d["coupling"],
        tool_errors=d["tool_errors"],
    )


def write_optimize_reports(result: OptimizeResult, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates").mkdir(exist_ok=True)
    (out / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    if result.static:
        (out / "analysis.md").write_text(
            render_analysis_markdown(_static_from_dict(result.static), result.profile), encoding="utf-8"
        )
    everyone = result.candidates + ([result.combination] if result.combination else [])
    for c in everyone:
        (out / "candidates" / f"{c.id}.md").write_text(render_candidate_report(c, result), encoding="utf-8")
        if c.patch:
            (out / "candidates" / f"{c.id}.patch").write_text(c.patch, encoding="utf-8")
    if result.measurement:
        write_reports(result.measurement, out / "measurement")
    if result.combination_measurement:
        write_reports(result.combination_measurement, out / "measurement-combined")
    report = out / "report.md"
    report.write_text(render_optimize_report(result), encoding="utf-8")
    (out / "report.html").write_text(
        render_optimize_html(result, version=generator_version()), encoding="utf-8"
    )
    return report
