"""Test suite script: examples, holdout check and evaluation (without Docker and LLM)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from joulecode.config import load_config
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.optimize import Pipeline
from joulecode.optimize_report import write_optimize_reports
from tests.fakes import PathRunner, ScriptedLLM

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("suite", ROOT / "scripts" / "suite.py")
suite = importlib.util.module_from_spec(_spec)
sys.modules["suite"] = suite
_spec.loader.exec_module(suite)

EXAMPLES = suite.load_suite(suite.DEFAULT_SUITE)
PY = sys.executable


def test_suite_file_is_valid():
    names = [e.name for e in EXAMPLES]
    assert "python-wordstats" in names and "python-checksum" in names
    for ex in EXAMPLES:
        cfg = load_config(ex.repo)  # joulecode.yml valid
        assert cfg.benchmark.min_duration_s >= 20
        assert ex.holdout is None or any(p.is_file() for p in ex.holdout.iterdir())
        if cfg.language != "python":
            assert ex.holdout is None or ex.holdout_command != suite.HOLDOUT_COMMAND, ex.name


# The following tests run tests locally with pytest – only for the Python examples.
PY_EXAMPLES = [e for e in EXAMPLES if load_config(e.repo).language == "python"]


@pytest.mark.parametrize("ex", [e for e in PY_EXAMPLES if e.holdout], ids=lambda e: e.name)
def test_original_passes_visible_and_holdout_tests(ex, tmp_path):
    if ex.name == "python-sigproc":
        pytest.importorskip("numpy")
    path = suite.prepare_copy(ex.repo, tmp_path / "copy", None, ex.holdout, ex.holdout_dir)
    cfg = load_config(ex.repo)
    box = suite.LocalRunner().start(str(path), cfg.limits)
    res = box.exec(f"{PY} -m {cfg.test.command} && {PY} -m {ex.holdout_command}", 120)
    assert res.ok, res.output


@pytest.mark.parametrize("ex", [e for e in PY_EXAMPLES if e.reference], ids=lambda e: e.name)
def test_reference_patch_passes_all_tests(ex, tmp_path):
    if ex.name == "python-sigproc":
        pytest.importorskip("numpy")
    path = suite.prepare_copy(ex.repo, tmp_path / "copy", ex.reference, ex.holdout, ex.holdout_dir)
    cfg = load_config(ex.repo)
    box = suite.LocalRunner().start(str(path), cfg.limits)
    res = box.exec(f"{PY} -m {cfg.test.command} && {PY} -m {ex.holdout_command}", 120)
    assert res.ok, res.output


def test_measure_args_keeps_only_options_measure_knows():
    extra = ["--model", "x", "--backend", "cgroup", "--repetitions=3", "--force", "--hotspots", "1"]
    assert suite.measure_args(extra) == ["--backend", "cgroup", "--repetitions=3", "--force"]


def _suite_dir(tmp_path: Path, examples: list) -> Path:
    out = tmp_path / "suite"
    out.mkdir()
    meta = {"started_at": "2026-10-07T20:00:00", "examples": [vars(e) for e in examples], "optimize_args": []}
    (out / "suite.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
    return out


def _fake_optimize(tmp_path: Path, out: Path) -> None:
    """Real pipeline run with fakes (as in test_agents) → real result.json."""
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "examples" / "python-wordstats", repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    pipe = Pipeline(
        cfg, PathRunner(), CgroupCpuBackend(), ScriptedLLM(), log=lambda _: None, wait_gpu=lambda: True
    )
    write_optimize_reports(pipe.run(repo, tmp_path / "work"), out)


def test_run_holdout_and_summary_on_real_pipeline_output(tmp_path):
    ex = next(e for e in EXAMPLES if e.name == "python-wordstats")
    out = _suite_dir(tmp_path, [ex])
    run_dir = out / "python-wordstats" / "run-1"
    _fake_optimize(tmp_path, run_dir)

    holdout = suite.run_holdout(ex, run_dir, suite.LocalRunner())
    # gate passed: two measured candidates, one veto, the combination
    assert len(holdout) == 4
    assert all(h["ok"] for h in holdout.values()), holdout

    summary = suite.write_summary(out)
    data = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    (run,) = data["runs"]
    assert run["status"] == "ok"
    assert run["hotspots_expected_hit"] == ["frequent_words", "unique_words"]
    assert run["by_status"] == {"measured": 2, "duplicate": 1, "vetoed": 1}
    assert run["repaired"] == 1 and run["repaired_ok"] == 1 and run["revisions"] == 1
    assert run["gate_leaks"] == []
    # the fake saves CPU time (FakeBox) – expectation improvement ≥ 30% met
    assert run["verdict"] == "MET", run["reason"]
    text = summary.read_text(encoding="utf-8")
    assert "1 of 1 runs" in text and "## Funnel" in text


def test_gate_leak_on_better_candidate_fails_the_run(tmp_path):
    ex = next(e for e in EXAMPLES if e.name == "python-wordstats")
    out = _suite_dir(tmp_path, [ex])
    run_dir = out / "python-wordstats" / "run-1"
    _fake_optimize(tmp_path, run_dir)
    data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    better = [r["id"] for r in data["ranking"] if r["verdict"] == "better"]
    # all better candidates fail the holdout → gate gap
    (run_dir / "holdout.json").write_text(
        json.dumps({cid: {"ok": False, "output": "FAILED test_lower_not_casefold"} for cid in better})
    )
    run = suite.evaluate_run(ex, run_dir)
    assert run["verdict"] == "NOT MET"
    assert "gate gap" in run["reason"]
    assert sorted(run["gate_leaks"]) == sorted(better)
    suite.write_summary(out)
    assert "## Gate gaps" in (out / "summary.md").read_text(encoding="utf-8")


def test_negative_control_flags_false_alarm(tmp_path):
    ex = next(e for e in EXAMPLES if e.name == "python-wordstats")
    ex = suite.Example(**{**vars(ex), "name": "control", "expect": "no_improvement", "tolerance": 0.05})
    out = _suite_dir(tmp_path, [ex])
    run_dir = out / "control" / "run-1"
    _fake_optimize(tmp_path, run_dir)
    run = suite.evaluate_run(ex, run_dir)
    assert run["verdict"] == "NOT MET"
    assert "false alarm" in run["reason"]


def test_no_hotspots_expectation(tmp_path):
    ex = next(e for e in EXAMPLES if e.name == "python-iowait")
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "no_hotspots",
                "error": "No function above the threshold",
                "profile": {"hotspots": []},
            }
        )
    )
    assert suite.evaluate_run(ex, run_dir)["verdict"] == "MET"
    (run_dir / "result.json").write_text(
        json.dumps({"status": "ok", "profile": {"hotspots": [{"qualname": "f"}]}})
    )
    run = suite.evaluate_run(ex, run_dir)
    assert run["verdict"] == "NOT MET" and "f" in run["reason"]


def test_missing_result_is_an_error(tmp_path):
    ex = EXAMPLES[0]
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    assert suite.evaluate_run(ex, run_dir)["verdict"] == "ERROR"
