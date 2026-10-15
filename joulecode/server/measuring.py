"""Processor for `gate` and `optimize`: the same measurement chain and pipeline as the command line.

gate      target branch (merge base) vs. PR head with `protocol.measure` – like
          `joulecode measure --candidate-dir`, except that both states are uploaded.
optimize  `optimize.Pipeline` on the uploaded state – like `joulecode optimize`.

Common to both:

* **Exclusive.** The worker is the only one anyway; in addition, the processor takes the
  command line's lock (`cleanup.acquire_lock`). If a `joulecode measure` started by hand is
  running, the job waits (at most `lock_wait_s`), and conversely a `joulecode measure`
  started by hand aborts while the server is measuring.
* **Progress.** Measurement chain and pipeline report via `log`. `LogBridge` writes every
  line to the job log, derives the phase from known line prefixes and checks on
  every line whether the job is to be cancelled (then `Cancelled` – the measurement chain cleans up
  back-end and containers in its `finally` blocks).
* **Reports.** The CLI's full reports (report.html, report.md, result.json, for
  optimize also candidate reports and patches) end up in the job directory under `report/` and are
  available via `/api/jobs/{id}/files/…`. The platform result summarises them.

Which side provides which files (gate): configuration and benchmark come from the
**target branch** – the PR cannot change what it is measured with. Tests, on the other hand, are
brought by each side: a PR that deliberately changes behaviour also changes its tests. In practice:
the protected paths from `joulecode.yml` are taken from the target branch, except
those whose name starts with “test” or “spec”.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from joulecode.server import RESULT_SCHEMA
from joulecode.server.processing import Cancelled, JobContext


class JobError(RuntimeError):
    """Error that ends the job; the message is written for humans."""


# -- Settings ----------------------------------------------------------------------------
def _docker_runner():
    from joulecode.container import DockerRunner

    return DockerRunner()


def _wait_gpu() -> bool:
    from joulecode.llm import wait_for_idle_gpu

    return wait_for_idle_gpu()


@dataclass
class MeasureSettings:
    """How `joulecode serve` measures (corresponds to the options of measure/optimize)."""

    backend: str = "auto"
    metrion_results: str | None = None
    rapl_idle_s: float = 10.0
    ollama_host: str | None = None
    model: str | None = None
    lock_wait_s: float = 3600.0
    use_lock: bool = True
    # replaceable for tests (without Docker, energy counters and Ollama)
    runner_factory: Callable[[], Any] = field(default=_docker_runner)
    backend_factory: Callable[[MeasureSettings], Any] | None = None
    llm_factory: Callable[[MeasureSettings], Any] | None = None
    wait_gpu: Callable[[], bool] = field(default=_wait_gpu)
    # adjusts the loaded ProjectConfig (tests: no pauses, few repetitions)
    configure: Callable[[Any], None] | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)

    def make_backend(self):
        if self.backend_factory:
            return self.backend_factory(self)
        from joulecode.energy import make_backend

        return make_backend(self.backend, metrion_results=self.metrion_results, rapl_idle_s=self.rapl_idle_s)

    def make_llm(self):
        if self.llm_factory:
            return self.llm_factory(self)
        from joulecode.llm import OllamaLLM

        return OllamaLLM(host=self.ollama_host, model=self.model)


# -- Progress and lock ---------------------------------------------------------------------
class LogBridge:
    """`log` callback for measurement chain/pipeline: log, phase, cancellation."""

    def __init__(self, ctx: JobContext, rules: list[tuple[str, str]], names: dict[str, str] | None = None):
        self.ctx = ctx
        self.names = names or {}
        self.rules = [(re.compile(p), phase) for p, phase in rules]
        self.current = -1

    def enter(self, phase: str) -> None:
        idx = self.ctx.phases.index(phase)
        if idx > self.current:
            self.current = idx
            self.ctx.phase(phase)

    def __call__(self, message: str) -> None:
        if self.ctx.is_cancelled():
            raise Cancelled
        for pattern, phase in self.rules:
            if pattern.match(message):
                self.enter(phase)
                break
        text = str(message).strip()
        for internal, shown in self.names.items():
            text = re.sub(rf"\b{internal}\b", shown, text)
        if text:
            level = "warn" if text.lower().startswith(("warning", "caution")) else "info"
            self.ctx.log(level, text)


class measurement_lock:
    """Take the command line's lock; wait while another joule:code run is measuring."""

    def __init__(self, ctx: JobContext, settings: MeasureSettings):
        self.ctx, self.settings = ctx, settings

    def __enter__(self):
        if not self.settings.use_lock:
            return self
        from joulecode.cleanup import acquire_lock, active_run

        deadline = time.monotonic() + self.settings.lock_wait_s
        announced = False
        while running := active_run():
            if not announced:
                self.ctx.log("warn", f"Measurement machine busy: {running} – job is waiting")
                announced = True
            if self.ctx.is_cancelled():
                raise Cancelled
            if time.monotonic() > deadline:
                raise JobError(
                    f"Measurement machine was busy for {self.settings.lock_wait_s:.0f} s ({running})."
                )
            self.settings.sleep(5)
        acquire_lock(f"serve (Job {self.ctx.job['id']})")
        return self

    def __exit__(self, *exc):
        if self.settings.use_lock:
            from joulecode.cleanup import release_lock

            release_lock()
        return False


def _ready_backend(settings: MeasureSettings, ctx: JobContext):
    backend = settings.make_backend()
    problems = backend.check()
    if problems:
        raise JobError(f"Measurement back-end '{backend.name}' not ready: " + "; ".join(problems))
    ctx.log("info", f"Backend: {backend.name} – {backend.quantity}")
    if hasattr(backend, "archive_dir") and ctx.report_dir:
        backend.archive_dir = ctx.report_dir / "metrion"  # raw data for `joulecode reanalyze`
    return backend


def _load_config(root: Path, where: str, settings: MeasureSettings | None = None):
    from pydantic import ValidationError

    from joulecode.config import load_config

    try:
        cfg = load_config(root)
    except FileNotFoundError as e:
        raise JobError(f"No joulecode.yml in the {where}.") from e
    except ValidationError as e:
        raise JobError(f"joulecode.yml in the {where} is invalid: {e}") from e
    if settings and settings.configure:
        settings.configure(cfg)
    return cfg


# -- Mapping of the CLI results ----------------------------------------------------------
def measurement_dict(m, variant: str, base: str = "baseline") -> dict[str, Any] | None:
    """Comparison of `variant` against `base` from a MeasurementResult in the platform schema."""
    comp = m.comparison.get(variant, {})
    prim = comp.get("primary")
    if not prim:
        return None
    sb, sc = m.summary.get(base, {}).get("primary", {}), m.summary.get(variant, {}).get("primary", {})
    unit = next((r.get("unit") for r in m.runs if r.get("unit")), "")
    secondary = {
        label: {"change": c["rel_change"], "ci95": [c["ci_low"], c["ci_high"]], "verdict": c["verdict"]}
        for label, c in comp.items()
        if label != "primary"
    }
    return {
        "backend": m.backend.get("name", ""),
        "quantity": m.backend.get("quantity", ""),
        "is_energy": m.backend.get("is_energy", True),
        "unit": unit,
        "baseline": {"median": sb.get("median"), "n": sb.get("n", 0), "cv": sb.get("cv")},
        "candidate": {"median": sc.get("median"), "n": sc.get("n", 0), "cv": sc.get("cv")},
        "change": prim["rel_change"],
        "ci95": [prim["ci_low"], prim["ci_high"]],
        "p_value": prim["p_value"],
        "verdict": prim["verdict"],
        "secondary": secondary,
        "runs": len(m.runs),
    }


def _files(report_dir: Path | None) -> dict[str, str]:
    """Downloadable report files (relative to report/)."""
    if not report_dir or not report_dir.is_dir():
        return {}
    wanted = {
        "report.html": "Measurement report (HTML)",
        "report.md": "Measurement report (Markdown)",
        "result.json": "Raw data (JSON)",
    }
    return {name: label for name, label in wanted.items() if (report_dir / name).is_file()}


def _base_result(mode: str, processor: str) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "dummy": False,
        "mode": mode,
        "processor": processor,
        "findings": [],
        "measurement": None,
        "candidates": [],
        "warnings": [],
    }


# -- gate --------------------------------------------------------------------------------------
GATE_PHASES = ["unpack", "prepare", "findings", "build images", "tests", "measurement", "analysis"]
GATE_RULES = [
    (r"Building ", "build images"),
    (r"Tests: ", "tests"),
    (r"Measuring with ", "measurement"),
    (r"Analysing measurement", "analysis"),
]
SIDE = {"baseline": "target branch", "candidate": "PR"}


def _side_names(text: str) -> str:
    for internal, shown in SIDE.items():
        text = re.sub(rf"\b{internal}\b", shown, text)
    return text


def is_test_path(path: str) -> bool:
    return any(part.lower().startswith(("test", "spec")) for part in Path(path).parts)


class GateProcessor:
    name = "gate"

    def __init__(self, settings: MeasureSettings | None = None):
        self.settings = settings or MeasureSettings()

    def phases(self, mode: str) -> list[str]:
        return GATE_PHASES

    def run(self, ctx: JobContext) -> dict[str, Any]:
        from joulecode.protocol import measure
        from joulecode.report import write_reports
        from joulecode.server.review import ReviewProcessor
        from joulecode.variants import Variant, _copy_tree, _restore_protected

        ctx.phase("unpack")
        ctx.phase("prepare")
        if ctx.base_dir is None:
            raise JobError("gate needs the state of the target branch (submit --base).")
        cfg = _load_config(
            ctx.base_dir, "target branch (measuring uses its configuration and benchmark)", self.settings
        )
        result = _base_result("gate", self.name)
        result["language"] = cfg.language

        work = ctx.workdir / "gate"
        baseline = Variant("baseline", work / "baseline")
        candidate = Variant("candidate", work / "candidate")
        _copy_tree(ctx.base_dir, baseline.path)
        _copy_tree(ctx.head_dir, candidate.path)
        shared = [p for p in cfg.protected_paths if not is_test_path(p)]
        _restore_protected(baseline.path, candidate, shared)
        result["warnings"] += [
            n.replace("changed in the candidate", "changed in the PR")
            + " Measuring uses the state of the target branch."
            for n in candidate.notes
        ]
        result["shared_paths"] = shared

        ctx.phase("findings")
        try:
            review = ReviewProcessor().analyze(ctx)
            for key in ("findings", "changed_functions", "metrics", "excluded"):
                result[key] = review.get(key)
            result["review_summary"] = review["summary"]
            result["warnings"] += [w for w in review["warnings"] if not w.startswith("The diff contains no")]
        except Exception as e:  # noqa: BLE001 - findings are a bonus, the measurement decides
            ctx.log("warn", f"Static analysis failed: {e}")
            result["warnings"].append(f"Findings not available: {e}")

        bridge = LogBridge(ctx, GATE_RULES, SIDE)
        out = ctx.report_dir
        with measurement_lock(ctx, self.settings):
            backend = _ready_backend(self.settings, ctx)
            m = measure(
                cfg,
                [baseline, candidate],
                self.settings.runner_factory(),
                backend,
                log=bridge,
                checkpoint=(lambda r: write_reports(r, out)) if out else None,
            )
        if out:
            write_reports(m, out)
        bridge.enter("analysis")

        result["tests"] = [
            {
                "variant": SIDE.get(g.variant, g.variant),
                "ok": g.ok,
                "wall_s": g.wall_s,
                "output_tail": g.output_tail,
            }
            for g in m.gates
        ]
        result["warnings"] += [_side_names(w) for w in m.warnings]
        result["files"] = _files(out)
        if m.status == "gate_failed":
            base_gate = next((g for g in m.gates if g.variant == "baseline"), None)
            if base_gate and not base_gate.ok:
                raise JobError(
                    "The tests already fail in the target branch – no comparison possible.\n"
                    + base_gate.output_tail
                )
            result["warnings"].append("The PR's tests fail – nothing was measured.")
        elif m.status != "ok":
            raise JobError(m.error or f"Measurement failed ({m.status})")
        else:
            result["measurement"] = measurement_dict(m, "candidate")
            if result["measurement"] is None:
                raise JobError("The measurement did not yield any usable values.")
        result["summary"] = _summary(result)
        return result


# -- optimize ----------------------------------------------------------------------------------
OPT_PHASES = [
    "unpack",
    "prepare",
    "original + tests",
    "analysis + profiling",
    "candidates",
    "measurement",
    "combination",
    "report",
]
OPT_RULES = [
    (r"Building original", "original + tests"),
    (r"Static analysis", "analysis + profiling"),
    (r"Hotspot ", "candidates"),
    (r"Measuring with ", "measurement"),
    (r"Combination", "combination"),
]
OPT_STATUS = {
    "no_hotspots": "No hotspots: no function is above optimize.min_share of the runtime.",
    "no_candidates": "No candidate passed tests and review.",
}


class OptimizeProcessor:
    name = "optimize"

    def __init__(self, settings: MeasureSettings | None = None):
        self.settings = settings or MeasureSettings()

    def phases(self, mode: str) -> list[str]:
        return OPT_PHASES

    def run(self, ctx: JobContext) -> dict[str, Any]:
        from joulecode.optimize import Pipeline
        from joulecode.optimize_report import write_optimize_reports

        ctx.phase("unpack")
        ctx.phase("prepare")
        cfg = _load_config(ctx.head_dir, "Repo", self.settings)
        opts = ctx.options
        if opts.get("hotspots"):
            cfg.optimize.hotspots = int(opts["hotspots"])
        if opts.get("candidates"):
            cfg.optimize.candidates_per_hotspot = int(opts["candidates"])
        llm = self.settings.make_llm()
        problems = llm.check() if hasattr(llm, "check") else []
        if problems:
            raise JobError(f"LLM {getattr(llm, 'name', '')} not ready: " + "; ".join(problems))
        ctx.log("info", f"LLM: {getattr(llm, 'name', '?')}")

        out = ctx.report_dir
        bridge = LogBridge(ctx, OPT_RULES)
        with measurement_lock(ctx, self.settings):
            backend = _ready_backend(self.settings, ctx)
            pipeline = Pipeline(
                cfg,
                self.settings.runner_factory(),
                backend,
                llm,
                log=bridge,
                wait_gpu=self.settings.wait_gpu,
                on_checkpoint=(lambda r: write_optimize_reports(r, out)) if out else None,
            )
            res = pipeline.run(ctx.head_dir, ctx.workdir / "optimize")
        bridge.enter("report")
        if out:
            write_optimize_reports(res, out)
            _write_patches(res, out)

        if res.status == "baseline_tests_failed":
            raise JobError("The original's tests fail – nothing to optimise.\n" + (res.error or ""))

        result = _base_result("optimize", self.name)
        result["language"] = res.language
        result["llm"] = res.llm
        result["warnings"] = list(res.warnings)
        if res.status in OPT_STATUS:
            result["warnings"].insert(0, OPT_STATUS[res.status] + (f" ({res.error})" if res.error else ""))
        elif res.status == "measurement_failed":
            result["warnings"].insert(0, f"Measurement failed: {res.error}")
        if res.measurement:
            result["warnings"] += res.measurement.warnings
        result["hotspots"] = [
            {k: h.get(k) for k in ("qualname", "file", "line", "share", "ncalls")}
            for h in (res.profile or {}).get("hotspots", [])
        ]
        result["candidates"] = _candidates(res, out)
        result["measurement"], result["best"] = _best(res)
        result["files"] = _files(out)
        result["summary"] = _summary(result)
        return result


def _write_patches(res, out: Path) -> None:
    patches = out / "patches"
    shutil.rmtree(patches, ignore_errors=True)
    patches.mkdir(parents=True)
    for c in [*res.candidates, *([res.combination] if res.combination else [])]:
        if c.patch:
            (patches / f"{c.id}.patch").write_text(c.patch, encoding="utf-8")


def _candidates(res, out: Path | None) -> list[dict[str, Any]]:
    ranking = {r["id"]: r for r in res.ranking}
    rows = []
    for c in [*res.candidates, *([res.combination] if res.combination else [])]:
        r = ranking.get(c.id)
        rows.append(
            {
                "id": c.id,
                "hotspot": c.hotspot,
                "summary": c.title,
                "rationale": c.rationale,
                "status": c.status,
                "origin": c.origin,
                "file": c.file,
                "functions": c.changed,
                "parts": c.parts,
                "change": r["rel_change"] if r else None,
                "ci95": [r["ci_low"], r["ci_high"]] if r else None,
                "verdict": r["verdict"] if r else None,
                "pareto": bool(r and r["pareto"]),
                "maintainability": (c.review or {}).get("score"),
                "concerns": ((c.review or {}).get("llm") or {}).get("concerns", [])
                + list((c.review or {}).get("rule_vetoes") or []),
                "messages": c.messages[-3:],
                "patch": c.patch or None,
                "patch_file": f"patches/{c.id}.patch" if out and c.patch else None,
            }
        )
    order = {"measured": 0, "ready": 1, "vetoed": 2, "tests_failed": 3, "duplicate": 4, "invalid": 5}
    rows.sort(
        key=lambda x: (order.get(x["status"], 9), x["change"] if x["change"] is not None else 9, x["id"])
    )
    return rows


def _best(res) -> tuple[dict | None, str | None]:
    """Measurement of the best candidate: the combination if it was measured and is better, otherwise the most efficient Pareto candidate."""
    combo = res.combination
    if combo and combo.status == "measured" and res.combination_measurement:
        m = measurement_dict(res.combination_measurement, combo.id)
        if m and m["verdict"] == "better":
            return m, combo.id
    better = [r for r in res.ranking if r["verdict"] == "better" and r["id"] != getattr(combo, "id", None)]
    if better and res.measurement:
        top = min(better, key=lambda r: r["rel_change"])
        return measurement_dict(res.measurement, top["id"]), top["id"]
    return None, None


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    from collections import Counter

    findings = result.get("findings") or []
    s: dict[str, Any] = {
        "findings": len(findings),
        "by_evidence": dict(Counter(f["evidence"] for f in findings)),
        "by_severity": dict(Counter(f["severity"] for f in findings)),
        "language": result.get("language"),
        "dummy": False,
    }
    if result.get("review_summary"):
        s["scope"] = result["review_summary"].get("scope")
        s["findings_outside_diff"] = result["review_summary"].get("findings_outside_diff", 0)
        s["changed_functions"] = result["review_summary"].get("changed_functions")
    m = result.get("measurement")
    if m:
        s.update(change=m["change"], ci95=m["ci95"], verdict=m["verdict"], backend=m["backend"])
    if result.get("tests"):
        s["tests_ok"] = all(t["ok"] for t in result["tests"])
    cands = result.get("candidates") or []
    if cands:
        s["candidates"] = len(cands)
        s["measured"] = sum(1 for c in cands if c["change"] is not None)
        s["better"] = sum(1 for c in cands if c["verdict"] == "better")
    return s
