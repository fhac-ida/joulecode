from __future__ import annotations

import random
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from joulecode.config import ProjectConfig, load_config
from joulecode.container import ExecResult
from joulecode.energy.base import Reading, RunHandle
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.energy.rapl import RaplBackend, _delta, discover_domains
from joulecode.protocol import build_schedule, measure
from joulecode.report import render_markdown
from joulecode.stats import compare, summarize
from joulecode.variants import prepare_variants

EXAMPLES = Path(__file__).parent.parent / "examples"
MINIMAL = {"test": {"command": "pytest"}, "benchmark": {"command": "python b.py"}}


# -- Configuration ----------------------------------------------------------
def test_example_config_loads():
    cfg = load_config(EXAMPLES / "python-wordstats")
    assert cfg.benchmark.env["BENCH_ROUNDS"] == "40"
    assert cfg.measurement.repetitions == 10


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({**MINIMAL, "benchmrak": {}})


def test_protected_paths_must_stay_inside_repo():
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({**MINIMAL, "protected_paths": ["../etc"]})


# -- Variants ---------------------------------------------------------------
def test_patch_applied_and_protected_paths_restored(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "code.py").write_text("x = 1\n")
    (repo / "tests" / "test_x.py").write_text("assert True\n")
    patch = tmp_path / "p.patch"
    patch.write_text(
        "--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-assert True\n+pass\n"
    )
    base, cand = prepare_variants(repo, tmp_path / "work", protected_paths=["tests"], candidate_patch=patch)
    assert (cand.path / "code.py").read_text() == "x = 2\n"
    assert (cand.path / "tests" / "test_x.py").read_text() == "assert True\n"
    assert any("tests" in n for n in cand.notes)
    assert (base.path / "code.py").read_text() == "x = 1\n"


def test_example_patch_applies(tmp_path):
    variants = prepare_variants(
        EXAMPLES / "python-wordstats",
        tmp_path,
        protected_paths=["tests", "bench", "joulecode.yml"],
        candidate_patch=EXAMPLES / "python-wordstats-optimized.patch",
    )
    assert "Counter" in (variants[1].path / "src/wordstats/core.py").read_text()
    assert variants[1].notes == []


def test_git_ref_export(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.txt").write_text("v1")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "1")
    (repo / "a.txt").write_text("v2")
    (base,) = prepare_variants(repo, tmp_path / "w", protected_paths=[], baseline_ref="HEAD")
    assert (base.path / "a.txt").read_text() == "v1"


# -- Statistics -------------------------------------------------------------
def test_clear_improvement_detected():
    rng = random.Random(1)
    base = [100 + rng.gauss(0, 2) for _ in range(10)]
    cand = [70 + rng.gauss(0, 2) for _ in range(10)]
    c = compare(base, cand)
    assert c.verdict == "better"
    assert -0.35 < c.rel_change < -0.25
    assert c.ci_high < 0


def test_identical_distributions_not_significant():
    rng = random.Random(2)
    base = [100 + rng.gauss(0, 5) for _ in range(10)]
    cand = [100 + rng.gauss(0, 5) for _ in range(10)]
    assert compare(base, cand).verdict == "no significant difference"


def test_summary():
    s = summarize([1.0, 2.0, 3.0])
    assert s.median == 2.0 and s.n == 3 and s.cv == pytest.approx(0.5)


# -- Schedule ---------------------------------------------------------------
def test_schedule_blocks_contain_each_variant_once():
    sched = build_schedule(["a", "b"], repetitions=5, warmup=1, rng=random.Random(0))
    assert len(sched) == 12
    assert all(w for _, w in sched[:2]) and not any(w for _, w in sched[2:])
    for i in range(0, 12, 2):
        assert {v for v, _ in sched[i : i + 2]} == {"a", "b"}


# -- RAPL -------------------------------------------------------------------
def _fake_rapl(root: Path, pkg: int, dram: int) -> None:
    z = root / "intel-rapl:0"
    d = z / "intel-rapl:0:0"
    d.mkdir(parents=True, exist_ok=True)
    (z / "energy_uj").write_text(str(pkg))
    (z / "max_energy_range_uj").write_text("1000000000")
    (d / "name").write_text("dram")
    (d / "energy_uj").write_text(str(dram))
    (d / "max_energy_range_uj").write_text("1000000000")


def test_rapl_discovery_and_wraparound(tmp_path):
    _fake_rapl(tmp_path, 0, 0)
    assert [d.key for d in discover_domains(tmp_path)] == ["package-0", "dram-0"]
    assert _delta(999_999_000, 1_000, 1_000_000_000) == 2_000


def test_rapl_backend_reading(tmp_path):
    _fake_rapl(tmp_path, 1_000_000, 500_000)
    b = RaplBackend(root=tmp_path, idle_seconds=0)
    assert b.check() == []
    b.begin_session()
    run = RunHandle("r1", "baseline", "c", False)
    b.begin_run(run)
    _fake_rapl(tmp_path, 4_000_000, 1_500_000)
    b.end_run(run)
    reading = b.finalize([run])["r1"]
    assert reading.value == pytest.approx(4.0)  # 3 J Package + 1 J DRAM
    assert reading.components == {"package-0_j": pytest.approx(3.0), "dram-0_j": pytest.approx(1.0)}


# -- metrion ----------------------------------------------------------------
def test_metrion_backend_sums_rows_of_container_cgroup(tmp_path):
    pl = pytest.importorskip("polars")
    from joulecode.energy.metrion_live import MetrionLiveBackend

    results = tmp_path / "energy_2026_10_05_10_00_00"
    results.mkdir()
    now = datetime.now(UTC).replace(tzinfo=None)
    cid = "a" * 64
    pl.DataFrame(
        {
            "window_start": [now - timedelta(seconds=20), now - timedelta(seconds=10), now],
            "window_end": [now - timedelta(seconds=10), now, now + timedelta(seconds=10)],
            "cgroup_name": [
                f"/system.slice/docker-{cid}.scope",
                f"/system.slice/docker-{cid}.scope",
                "/user.slice",
            ],
            "cpu_dynamic_j": [3.0, 2.0, 100.0],
            "dram_dynamic_j": [0.5, 0.5, 10.0],
            "cpu_total_j": [5.0, 4.0, 200.0],
        }
    ).write_parquet(results / "part-0.parquet")

    b = MetrionLiveBackend(results=str(tmp_path / "energy*"), window_s=0, max_wait_s=1, poll_s=0.1)
    assert b.check() == []
    b.directory = results  # session started before the rows
    run = RunHandle("r1", "baseline", cid, False, t_end=datetime.now(UTC) - timedelta(seconds=15))
    reading = b.finalize([run])["r1"]
    assert reading.value == pytest.approx(6.0)
    assert reading.components["cpu_total_j"] == pytest.approx(9.0)


# -- Protocol with fakes -----------------------------------------------------
class FakeBox:
    def __init__(self, image: str, runner: FakeRunner) -> None:
        self.id = f"{image}-{len(runner.started)}"
        self.image, self.runner, self.cpu = image, runner, 0.0

    def exec(self, command, timeout_s, env=None):
        if command == "pytest":
            ok = self.image not in self.runner.failing_tests
            return ExecResult(0 if ok else 1, "1 passed" if ok else "1 failed", 0.1)
        self.cpu += self.runner.cost[self.image]
        return ExecResult(0, "ok", self.runner.cost[self.image])

    def cpu_seconds(self):
        return self.cpu

    def remove(self):
        pass


class FakeRunner:
    def __init__(self, cost: dict[str, float], failing_tests: set[str] = frozenset()) -> None:
        self.cost, self.failing_tests, self.started = cost, failing_tests, []

    def build(self, context, dockerfile, tag):
        return tag.split(":")[0].split("/")[1]  # "baseline" / "candidate"

    def start(self, image, limits):
        box = FakeBox(image, self)
        self.started.append(box)
        return box


def _cfg(**measurement) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            **MINIMAL,
            "measurement": {"repetitions": 4, "cooldown_s": 0, "seed": 1, **measurement},
            "benchmark": {"command": "bench", "min_duration_s": 0},
        }
    )


def _variants(tmp_path):
    from joulecode.variants import Variant

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "x").write_text("1")
    return [Variant("baseline", tmp_path / "b"), Variant("candidate", tmp_path / "c")]


def test_protocol_full_run(tmp_path):
    runner = FakeRunner({"baseline": 2.0, "candidate": 1.0})
    result = measure(_cfg(), _variants(tmp_path), runner, CgroupCpuBackend(), log=lambda _: None)
    assert result.status == "ok"
    assert len(result.runs) == 10  # 1 warmup block + 4 repetitions, 2 variants each
    assert result.summary["candidate"]["primary"]["median"] == pytest.approx(1.0)
    assert result.comparison["candidate"]["primary"]["rel_change"] == pytest.approx(-0.5)
    assert "Comparison" in render_markdown(result)
    # 2 test containers + 10 measurement containers, every run fresh
    assert len(runner.started) == 12


def test_protocol_stops_when_candidate_breaks_tests(tmp_path):
    runner = FakeRunner({"baseline": 2.0, "candidate": 1.0}, failing_tests={"candidate"})
    result = measure(_cfg(), _variants(tmp_path), runner, CgroupCpuBackend(), log=lambda _: None)
    assert result.status == "gate_failed"
    assert result.runs == []
    assert "failed" in render_markdown(result)


# -- Adaptive repetitions ----------------------------------------------------------
class CyclingRunner(FakeRunner):
    """Cost per variant as a list that is cycled through (spread)."""

    def __init__(self, costs: dict[str, list[float]]) -> None:
        super().__init__({k: v[0] for k, v in costs.items()})
        self.costs, self.calls = costs, {k: 0 for k in costs}

    def start(self, image, limits):
        box = super().start(image, limits)
        runner = self

        def exec_(command, timeout_s, env=None):
            seq = runner.costs[image]
            cost = seq[runner.calls[image] % len(seq)]
            runner.calls[image] += 1
            box.cpu += cost
            return ExecResult(0, "ok", cost)

        box.exec = exec_
        return box


def test_adaptive_stops_clear_difference_after_min_repetitions(tmp_path):
    runner = FakeRunner({"baseline": 2.0, "candidate": 1.0})
    logs = []
    result = measure(
        _cfg(repetitions=10, min_repetitions=5),
        _variants(tmp_path),
        runner,
        CgroupCpuBackend(),
        log=logs.append,
    )
    assert len(result.runs) == 2 + 5 * 2  # warmup + 5 instead of 10 blocks
    assert result.plan["stopped"]["candidate"] == {"after_blocks": 5, "cpu_verdict": "better"}
    assert result.comparison["candidate"]["primary"]["verdict"] == "better"
    assert any("decided after 5" in x for x in logs)


def test_adaptive_stops_equal_candidates_as_equal(tmp_path):
    runner = FakeRunner({"baseline": 2.0, "candidate": 2.0})
    result = measure(_cfg(repetitions=10, min_repetitions=5), _variants(tmp_path), runner, CgroupCpuBackend())
    assert result.plan["stopped"]["candidate"]["cpu_verdict"] == "equal"
    assert len(result.runs) == 12


def test_adaptive_measures_to_maximum_when_noisy(tmp_path):
    runner = CyclingRunner({"baseline": [1.0, 1.2, 1.1], "candidate": [1.05, 1.15, 1.0]})
    result = measure(
        _cfg(repetitions=8, min_repetitions=5),
        _variants(tmp_path),
        runner,
        CgroupCpuBackend(),
        log=lambda _: None,
    )
    assert "candidate" not in result.plan.get("stopped", {})
    assert len(result.runs) == 2 + 8 * 2


def test_fixed_mode_and_small_repetitions_measure_all_blocks(tmp_path):
    runner = FakeRunner({"baseline": 2.0, "candidate": 1.0})
    result = measure(_cfg(repetitions=10, adaptive=False), _variants(tmp_path), runner, CgroupCpuBackend())
    assert len(result.runs) == 22
    # repetitions < min_repetitions: no early stop possible
    result = measure(_cfg(repetitions=4), _variants(tmp_path / "2"), runner, CgroupCpuBackend())
    assert len(result.runs) == 10


def test_stop_alpha_allows_stop_at_five_with_complete_separation():
    from scipy.stats import mannwhitneyu

    from joulecode.protocol import stop_alpha

    a = stop_alpha(0.05, 5, 10)
    assert a == pytest.approx(0.05 / 6)
    # the smallest possible p for 5 vs. 5 runs must lie below it, otherwise nothing ever stops
    assert mannwhitneyu([2, 2.1, 2.2, 2.3, 2.4], [1, 1.1, 1.2, 1.3, 1.4]).pvalue < a


class DroppingBackend(CgroupCpuBackend):
    """Like cgroup, but in the first session a candidate is missing three values (attribution dropout)."""

    name = "dropping"

    def __init__(self) -> None:
        self.sessions = 0

    def begin_session(self) -> None:
        self.sessions += 1

    def finalize(self, runs):
        out = super().finalize(runs)
        if self.sessions == 1:
            dropped = [r.run_id for r in runs if r.variant == "candidate" and not r.warmup][:3]
            for rid in dropped:
                out[rid] = Reading(None, "CPU-s", note="attribution dropout")
        return out


def test_topup_session_when_backend_lost_runs(tmp_path):
    backend = DroppingBackend()
    logs = []
    result = measure(
        _cfg(repetitions=10, min_repetitions=5), _variants(tmp_path), FakeRunner({"baseline": 2.0, "candidate": 1.0}),
        backend, log=logs.append,
    )  # fmt: skip
    assert backend.sessions == 2
    assert result.plan["topup"]["blocks"] == 3 and result.plan["topup"]["valid_before"] == {"candidate": 2}
    second = [r for r in result.runs if r["session"] == 2]
    assert len(second) == (1 + 3) * 2  # warmup + 3 blocks, the original runs along
    assert result.summary["candidate"]["primary"]["n"] == 5
    assert any(x.startswith("Top-up measurement") and "candidate: 2 of 5 valid" in x for x in logs)
    from joulecode.protocol import repetitions_text

    assert "top-up measurement 3 block(s)" in repetitions_text(result)
