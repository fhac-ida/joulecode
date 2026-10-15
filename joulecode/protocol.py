"""Measurement protocol: build → test gate → warmup → interleaved repetitions → statistics.

Against systematic errors:
* Every repetition is a block in which every variant runs exactly once in
  random order. Drift (warming up, background jobs) thus affects
  all variants equally.
* Warmup runs (caches, turbo state) are measured but not counted.
* A pause between runs so that consumption returns to idle.
* A fresh container per run so that no state is left over.
* Tests must be green in all variants, otherwise nothing is measured at all.
* Plausibility: a run to which the back-end attributes hardly any energy although it used a
  normal amount of CPU time is a measurement dropout and is discarded (see
  `flag_implausible`). The CPU time comes from the container's cgroup and is
  independent of the energy back-end.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from joulecode.config import ProjectConfig
from joulecode.container import Runner, image_tag
from joulecode.energy.base import EnergyBackend, RunHandle
from joulecode.hostcheck import gpu_utilization
from joulecode.stats import compare, early_decision, summarize
from joulecode.variants import Variant

Log = Callable[[str], None]


@dataclass
class GateResult:
    variant: str
    ok: bool
    exit_code: int
    wall_s: float
    output_tail: str


@dataclass
class MeasurementResult:
    status: str  # "ok" | "gate_failed" | "benchmark_failed"
    started_at: str
    finished_at: str | None = None
    backend: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    variants: list[dict] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    # {candidate: {metric: comparison dict}} – each against the original
    comparison: dict = field(default_factory=dict)
    # Actual course: candidates decided early, top-up measurement, number of runs
    plan: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _now() -> datetime:
    return datetime.now(UTC)


def run_gate(runner: Runner, images: dict[str, str], cfg: ProjectConfig, log: Log) -> list[GateResult]:
    results = []
    for name, image in images.items():
        log(f"Tests: {name} …")
        box = runner.start(image, cfg.limits)
        try:
            res = box.exec(cfg.test.command, cfg.test.timeout_s, cfg.test.env)
        finally:
            box.remove()
        results.append(GateResult(name, res.ok, res.exit_code, res.wall_s, _tail(res.output)))
        log(f"Tests: {name} {'passed' if res.ok else f'FAILED (exit {res.exit_code})'}")
    return results


def build_schedule(
    variants: list[str], repetitions: int, warmup: int, rng: random.Random
) -> list[tuple[str, bool]]:
    schedule: list[tuple[str, bool]] = []
    for is_warmup, blocks in ((True, warmup), (False, repetitions)):
        for _ in range(blocks):
            block = variants[:]
            rng.shuffle(block)
            schedule.extend((v, is_warmup) for v in block)
    return schedule


def measure(
    cfg: ProjectConfig,
    variants: list[Variant],
    runner: Runner,
    backend: EnergyBackend,
    *,
    bench_env: dict[str, str] | None = None,
    log: Log = print,
    sleep: Callable[[float], None] = time.sleep,
    checkpoint: Callable[[MeasurementResult], None] | None = None,
) -> MeasurementResult:
    m = cfg.measurement
    if hasattr(backend, "log"):
        backend.log = log
    result = MeasurementResult(status="ok", started_at=_now().isoformat(), config=cfg.model_dump())
    result.variants = [{"name": v.name, "notes": v.notes} for v in variants]

    # 1. Build
    images: dict[str, str] = {}
    for v in variants:
        tag = image_tag(v.name, v.path)
        log(f"Building {v.name} → {tag}")
        images[v.name] = runner.build(v.path / cfg.build.context, cfg.build.dockerfile, tag)
    for entry in result.variants:
        entry["image"] = images[entry["name"]]

    # 2. Test-Gate
    result.gates = run_gate(runner, images, cfg, log)
    if not all(g.ok for g in result.gates):
        result.status = "gate_failed"
        result.finished_at = _now().isoformat()
        return result

    # 3. Measurement
    util = gpu_utilization()
    if util and any(u > 5 for u in util):
        result.warnings.append(
            f"The GPU was busy before the measurement ({util}%). Was an LLM running in parallel?"
        )

    env = {**cfg.benchmark.env, **(bench_env or {})}
    rng = random.Random(m.seed)
    names = [v.name for v in variants]
    planner = BlockPlanner(names, m, rng)
    log(
        f"Measuring with back-end '{backend.name}': {m.warmup} warmup block(s), "
        + (
            f"{planner.min_reps}–{m.repetitions} repetitions per variant (adaptive)"
            if planner.adaptive
            else f"{m.repetitions} repetitions"
        )
    )
    session = _Session(cfg, images, runner, backend, env, log, sleep, result, checkpoint)
    readings = session.run(planner.next_block, session_no=1)
    if readings is None:  # benchmark or analysis error, recorded in result
        _apply_readings(result, session.readings)
        result.finished_at = _now().isoformat()
        if result.status == "evaluation_failed":
            _evaluate(result, names, cfg)
        return result
    _apply_readings(result, readings)

    # 4. Top-up measurement if the back-end discarded too many runs
    need = _missing_valid(result, names, planner.min_reps) if m.topup and planner.adaptive else {}
    if need:
        blocks = planner.min_reps - min(need.values())
        extra = [names[0]] + [n for n in names[1:] if n in need]
        log(
            "Top-up measurement: too few valid runs ("
            + ", ".join(f"{n}: {k} of {planner.min_reps} valid" for n, k in need.items())
            + f") – {blocks} more block(s) with {', '.join(extra)}"
        )
        result.plan["topup"] = {"variants": extra, "blocks": blocks, "valid_before": need}
        topup = TopupPlanner(extra, blocks, m.warmup, rng)
        more = session.run(topup.next_block, session_no=2)
        if more is None:
            result.warnings.append(f"Top-up measurement failed: {result.error}")
            result.status, result.error = "ok", None
            more = {}
        readings = {**readings, **more}
        _apply_readings(result, readings)

    result.backend = backend.describe()
    _evaluate(result, names, cfg)
    result.finished_at = _now().isoformat()
    return result


def repetitions_text(result: MeasurementResult) -> str:
    """How often was actually measured – for the reports (older results without `plan`: fixed)."""
    m = result.config.get("measurement", {})
    counts = {}
    for r in result.runs:
        if not r.get("warmup"):
            counts[r["variant"]] = counts.get(r["variant"], 0) + 1
    if not result.plan or not counts:
        return f"{m.get('repetitions')} repetitions"
    lo, hi = min(counts.values()), max(counts.values())
    text = f"{lo}–{hi} repetitions per variant" if lo != hi else f"{hi} repetitions"
    stopped = result.plan.get("stopped", {})
    if stopped:
        text += (
            " (adaptive; decided early: "
            + ", ".join(f"{n} after {d['after_blocks']}" for n, d in stopped.items())
            + ")"
        )
    topup = result.plan.get("topup")
    if topup:
        text += f", top-up measurement {topup['blocks']} block(s) because of discarded runs"
    return text


def stop_alpha(alpha: float, min_reps: int, max_reps: int) -> float:
    """Significance level for early stopping: Bonferroni over all interim looks.

    Whoever looks after every block and stops at p < alpha would otherwise find a difference
    more often than alpha promises. The final analysis uses the full alpha again.
    """
    return alpha / max(1, max_reps - min_reps + 1)


class BlockPlanner:
    """Yields the blocks of a measurement campaign; in adaptive mode decided candidates drop out.

    Decisions are based on CPU time: it is available right after every run, whereas energy
    is only available with metrion after the session ends. For compute-heavy benchmarks energy
    closely follows CPU time (measured: −84.6% energy at −84.2% CPU time). The
    original runs in every block so that drift still affects all variants equally.
    """

    def __init__(self, names: list[str], m, rng: random.Random) -> None:
        self.names, self.m, self.rng = names, m, rng
        self.min_reps = min(m.min_repetitions, m.repetitions)
        self.adaptive = m.adaptive and self.min_reps < m.repetitions and len(names) > 1
        self.active = list(names[1:])
        self.alpha = stop_alpha(m.alpha, self.min_reps, m.repetitions)
        self.blocks_done = 0  # counted blocks
        self.warmups_done = 0
        self.decisions: dict[str, dict] = {}

    def next_block(self, runs: list[RunHandle]) -> list[tuple[str, bool]] | None:
        if self.warmups_done < self.m.warmup:
            self.warmups_done += 1
            return self._shuffled(self.names, warm=True)
        if self.blocks_done >= self.min_reps and self.adaptive:
            self._decide(runs)
        if self.blocks_done >= self.m.repetitions or (self.names[1:] and not self.active):
            return None
        self.blocks_done += 1
        return self._shuffled([self.names[0], *self.active], warm=False)

    def _decide(self, runs: list[RunHandle]) -> None:
        def cpu(name: str) -> list[float]:
            return [r.cpu_s for r in runs if r.variant == name and not r.warmup and r.cpu_s is not None]

        base = cpu(self.names[0])
        for name in list(self.active):
            verdict = early_decision(
                base, cpu(name), alpha=self.alpha, precision=self.m.precision, seed=self.m.seed
            )
            if verdict:
                self.active.remove(name)
                self.decisions[name] = {"after_blocks": self.blocks_done, "cpu_verdict": verdict}

    def _shuffled(self, names: list[str], *, warm: bool) -> list[tuple[str, bool]]:
        block = names[:]
        self.rng.shuffle(block)
        return [(n, warm) for n in block]


class TopupPlanner:
    """Fixed number of additional blocks for the top-up measurement (own session, own warmup)."""

    def __init__(self, names: list[str], blocks: int, warmup: int, rng: random.Random) -> None:
        self.plan = build_schedule(names, blocks, warmup, rng)
        self.size = len(names)
        self.pos = 0

    def next_block(self, runs: list[RunHandle]) -> list[tuple[str, bool]] | None:
        if self.pos >= len(self.plan):
            return None
        block = self.plan[self.pos : self.pos + self.size]
        self.pos += self.size
        return block


def _missing_valid(result: MeasurementResult, names: list[str], min_reps: int) -> dict[str, int]:
    """Variants with fewer than `min_reps` valid energy values → number of valid ones."""
    out = {}
    for name in names:
        scored = [r for r in result.runs if r["variant"] == name and not r["warmup"]]
        valid = sum(1 for r in scored if r["value"] is not None)
        if scored and valid < min_reps:
            out[name] = valid
    return out


class _Session:
    """One session of the energy back-end: execute runs, then analyse (with metrion: stop the service)."""

    def __init__(self, cfg, images, runner, backend, env, log, sleep, result, checkpoint) -> None:
        self.cfg, self.images, self.runner, self.backend = cfg, images, runner, backend
        self.env, self.log, self.sleep, self.result, self.checkpoint = env, log, sleep, result, checkpoint
        self.readings: dict = {}
        self.counter = len(result.runs)

    def _report_decisions(self, next_block) -> None:
        planner = getattr(next_block, "__self__", None)
        if not isinstance(planner, BlockPlanner) or not planner.decisions:
            return
        stopped = self.result.plan.setdefault("stopped", {})
        for name, d in planner.decisions.items():
            if name not in stopped:
                stopped[name] = d
                self.log(f"{name}: decided after {d['after_blocks']} blocks ({d['cpu_verdict']}, CPU time)")

    def run(self, next_block, *, session_no: int) -> dict | None:
        cfg, result, log, backend = self.cfg, self.result, self.log, self.backend
        m = cfg.measurement
        deferred = []  # containers that are removed only after the analysis
        handles: list[RunHandle] = []
        try:
            backend.begin_session()
            result.backend = backend.describe()
            first = True
            while (block := next_block(handles)) is not None:
                for name, warm in block:
                    if not first:
                        self.sleep(m.cooldown_s)
                    first = False
                    self.counter += 1
                    box = self.runner.start(self.images[name], cfg.limits)
                    try:
                        run = RunHandle(
                            run_id=f"r{self.counter:03d}", variant=name, container_id=box.id, warmup=warm
                        )
                        cpu0 = box.cpu_seconds()
                        run.t_start = _now()
                        backend.begin_run(run)
                        res = box.exec(cfg.benchmark.command, cfg.benchmark.timeout_s, self.env)
                        backend.end_run(run)
                        run.t_end = _now()
                        cpu1 = box.cpu_seconds()
                    finally:
                        if backend.defer_cleanup:
                            deferred.append(box)
                        else:
                            box.remove()
                    run.wall_s = res.wall_s
                    run.cpu_s = cpu1 - cpu0 if cpu0 is not None and cpu1 is not None else None
                    log(
                        f"[{self.counter}] {name}{' (Warmup)' if warm else ''}: "
                        f"{res.wall_s:.2f} s, CPU {run.cpu_s if run.cpu_s is None else f'{run.cpu_s:.2f} s'}"
                    )
                    if not res.ok:
                        result.status = "benchmark_failed"
                        result.error = (
                            f"Benchmark {name} ended with exit {res.exit_code}:\n{_tail(res.output)}"
                        )
                        result.runs += [_run_record(h, session_no) for h in handles]
                        return None
                    handles.append(run)
                self._report_decisions(next_block)
            self._report_decisions(next_block)

            # Save the runs before the back-end analyses: if the analysis aborts
            # (error, Ctrl+C), it can be repeated with `joulecode reanalyze`.
            result.runs += [_run_record(run, session_no) for run in handles]
            result.backend = backend.describe()
            result.plan["runs"] = len(result.runs)
            if self.checkpoint:
                try:
                    self.checkpoint(result)
                except Exception as exc:  # noqa: BLE001 - saving intermediate state must never end the measurement
                    log(f"Warning: intermediate state could not be saved: {exc}")

            log("Analysing measurement …")
            try:
                self.readings = backend.finalize(handles)
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - runs are to be preserved
                reason = "aborted (Ctrl+C)" if isinstance(exc, KeyboardInterrupt) else f"failed: {exc}"
                result.status = "evaluation_failed"
                result.error = (
                    f"Analysis by the back-end '{backend.name}' {reason}\n"
                    "Wall time and CPU time are preserved. Repeat without a new measurement with: "
                    "joulecode reanalyze <result-directory>"
                )
                log(f"Analysis {reason}")
                return None
            return self.readings
        finally:
            backend.close()
            if deferred:
                log(f"Cleaning up {len(deferred)} measurement containers …")
            for box in deferred:
                box.remove()


def _run_record(run: RunHandle, session: int = 1) -> dict:
    return {
        "run_id": run.run_id,
        "session": session,
        "variant": run.variant,
        "warmup": run.warmup,
        "container_id": run.container_id,
        "t_start": run.t_start.isoformat() if run.t_start else None,
        "t_end": run.t_end.isoformat() if run.t_end else None,
        "wall_s": run.wall_s,
        "cpu_s": run.cpu_s,
        # placeholder until the back-end has analysed
        "value": None,
        "unit": None,
        "components": {},
        "note": "not analysed yet",
    }


def _apply_readings(result: MeasurementResult, readings: dict) -> None:
    for run in result.runs:
        reading = readings.get(run["run_id"])
        run["value"] = reading.value if reading else None
        run["unit"] = reading.unit if reading else None
        run["components"] = reading.components if reading else {}
        run["note"] = reading.note if reading else "no measurement value"
        run.pop("value_raw", None)
        run.pop("discarded", None)
    flag_implausible(result)


# Energy per CPU second: for identical code on a fixed core it varies by a few
# per cent. A factor of 2 up or down is no longer measurement noise.
PLAUSIBLE_RATIO = (0.5, 2.0)


def flag_implausible(result: MeasurementResult) -> int:
    """Discard runs whose energy does not match the measured CPU time.

    Observed with metrion: individual runs are attributed only ~1 J instead of several hundred J
    although the container used the full CPU time. The reference is
    the median of energy/CPU time of the same variant (different variants may
    compute at different “heat”); if that median itself lies far below the one of the whole
    campaign (variant consists almost only of dropouts), the campaign median applies.
    The original value is kept as `value_raw`. Returns the number of
    discarded runs.
    """
    if not result.backend.get("is_energy", True):
        return 0  # CPU time as proxy: the ratio is 1 by definition

    def ratio(r: dict) -> float | None:
        cpu = r.get("cpu_s")
        return r["value"] / cpu if r.get("value") is not None and cpu and cpu > 0 else None

    all_ratios = [q for r in result.runs if (q := ratio(r)) is not None]
    if len(all_ratios) < 3:
        return 0
    campaign = statistics.median(all_ratios)
    flagged = 0
    for variant in dict.fromkeys(r["variant"] for r in result.runs):
        runs = [r for r in result.runs if r["variant"] == variant and ratio(r) is not None]
        if len(runs) < 3:
            continue
        ref = statistics.median(ratio(r) for r in runs)
        if ref < PLAUSIBLE_RATIO[0] * campaign:
            ref = campaign
        if ref <= 0:
            continue
        for r in runs:
            q = ratio(r) / ref
            if PLAUSIBLE_RATIO[0] <= q <= PLAUSIBLE_RATIO[1]:
                continue
            expected = ref * r["cpu_s"]
            r["value_raw"], r["value"], r["discarded"] = r["value"], None, True
            r["note"] = (
                f"discarded: {r['value_raw']:.3g} {r.get('unit') or ''} at {r['cpu_s']:.1f} s CPU time, "
                f"expected ≈ {expected:.3g} – attribution dropout of the back-end"
            )
            flagged += 1
    return flagged


def run_handles(result: MeasurementResult) -> list[RunHandle]:
    """The runs of a saved result as RunHandles (for a new analysis)."""
    return [
        RunHandle(
            run_id=r["run_id"],
            variant=r["variant"],
            container_id=r["container_id"],
            warmup=r["warmup"],
            t_start=datetime.fromisoformat(r["t_start"]) if r.get("t_start") else None,
            t_end=datetime.fromisoformat(r["t_end"]) if r.get("t_end") else None,
            wall_s=r.get("wall_s"),
            cpu_s=r.get("cpu_s"),
        )
        for r in result.runs
    ]


def reevaluate(result: MeasurementResult, readings: dict, cfg: ProjectConfig) -> None:
    """Insert new measurement values (e.g. from an archived metrion database) and analyse again."""
    _apply_readings(result, readings)
    result.summary, result.comparison = {}, {}
    result.warnings = [w for w in result.warnings if w.startswith("GPU")]
    result.status, result.error = "ok", None
    _evaluate(result, [v["name"] for v in result.variants], cfg)


def load_result(data: dict) -> MeasurementResult:
    fields = dict(data)
    fields["gates"] = [GateResult(**g) for g in data.get("gates", [])]
    return MeasurementResult(**fields)


METRICS = (("primary", "value"), ("wall_s", "wall_s"), ("cpu_s", "cpu_s"))


def _series(result: MeasurementResult, variant: str, key: str) -> list[float]:
    return [r[key] for r in result.runs if r["variant"] == variant and not r["warmup"] and r[key] is not None]


def _evaluate(result: MeasurementResult, names: list[str], cfg: ProjectConfig) -> None:
    m = cfg.measurement
    for name in names:
        result.summary[name] = {}
        for label, key in METRICS:
            values = _series(result, name, key)
            if values:
                result.summary[name][label] = summarize(values).to_dict()
        scored = [r for r in result.runs if r["variant"] == name and not r["warmup"]]
        discarded = sum(1 for r in scored if r.get("discarded"))
        missing = sum(1 for r in scored if r["value"] is None and not r.get("discarded"))
        if missing:
            result.warnings.append(
                f"{name}: {missing} runs without a measurement value (see 'note' in runs)."
            )
        if discarded:
            result.warnings.append(
                f"{name}: {discarded} of {len(scored)} runs discarded – the back-end attributed hardly any "
                "energy to them although they used a normal amount of CPU time (attribution dropout). "
                "The remaining runs are analysed."
            )

        wall = result.summary[name].get("wall_s")
        if wall and wall["median"] < cfg.benchmark.min_duration_s:
            if name == names[0]:
                result.warnings.append(
                    f"{name}: the benchmark takes only {wall['median']:.1f} s (median) "
                    f"(< {cfg.benchmark.min_duration_s:.0f} s). Too short for RAPL/metrion – increase the load."
                )
            else:
                result.warnings.append(
                    f"{name}: runs only {wall['median']:.1f} s long. With 1 s sampling the absolute value is "
                    "uncertain; the direction of the comparison remains meaningful."
                )
        prim = result.summary[name].get("primary")
        if prim and prim["cv"] > 0.10:
            result.warnings.append(
                f"{name}: high spread (coefficient of variation {prim['cv']:.0%}). Check background load, use more repetitions."
            )

    # Every further variant is compared with the first one (the original).
    base = names[0]
    for cand in names[1:]:
        result.comparison[cand] = {}
        for label, key in METRICS:
            b, c = _series(result, base, key), _series(result, cand, key)
            if len(b) >= 2 and len(c) >= 2:
                result.comparison[cand][label] = compare(b, c, alpha=m.alpha, seed=m.seed).to_dict()
