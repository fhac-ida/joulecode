"""gate and optimize on the platform – with the fakes from tests/fakes.py (without Docker/Ollama)."""

from __future__ import annotations

import io
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.server import gate as gate_rules
from joulecode.server.measuring import (
    GATE_PHASES,
    OPT_PHASES,
    GateProcessor,
    JobError,
    MeasureSettings,
    OptimizeProcessor,
    is_test_path,
)
from joulecode.server.processing import Cancelled, JobContext
from tests.fakes import PathRunner, ScriptedLLM

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "python-wordstats"
PATCH = ROOT / "examples" / "python-wordstats-optimized.patch"


def fast(cfg) -> None:
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0


def settings(**kw) -> MeasureSettings:
    defaults = dict(
        runner_factory=PathRunner,
        backend_factory=lambda s: CgroupCpuBackend(),
        llm_factory=lambda s: ScriptedLLM(),
        wait_gpu=lambda: True,
        configure=fast,
        use_lock=False,
        sleep=lambda s: None,
    )
    defaults.update(kw)
    return MeasureSettings(**defaults)


def optimized(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst)
    subprocess.run(["git", "apply", str(PATCH)], cwd=dst, check=True, capture_output=True)
    return dst


class Recorder:
    def __init__(self, cancel_after: int | None = None):
        self.phases: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.cancel_after = cancel_after
        self.checks = 0

    def cancelled(self) -> bool:
        self.checks += 1
        return self.cancel_after is not None and self.checks > self.cancel_after


def context(
    tmp: Path, mode: str, head: Path, base: Path | None = None, rec: Recorder | None = None, **options
):
    rec = rec or Recorder()
    phases = GATE_PHASES if mode == "gate" else OPT_PHASES
    return (
        JobContext(
            job={"id": "job1", "mode": mode, "options": options, "changed_files": []},
            workdir=tmp / "work",
            head_dir=head,
            base_dir=base,
            report_phase=lambda name, p: rec.phases.append(name),
            log=lambda level, msg: rec.logs.append((level, msg)),
            is_cancelled=rec.cancelled,
            phases=phases,
            report_dir=tmp / "report",
        ),
        rec,
    )


# -- gate -----------------------------------------------------------------------------------
def test_gate_measures_pr_against_target(tmp_path):
    head = optimized(tmp_path / "head")
    ctx, rec = context(tmp_path, "gate", head, EXAMPLE)
    result = GateProcessor(settings()).run(ctx)

    m = result["measurement"]
    assert m["verdict"] == "better" and m["change"] < -0.5
    assert m["backend"] == "cgroup" and m["is_energy"] is False and m["unit"] == "CPU-s"
    assert m["baseline"]["n"] >= 2 and "cpu_s" in m["secondary"]
    assert [t["variant"] for t in result["tests"]] == ["target branch", "PR"] and all(
        t["ok"] for t in result["tests"]
    )
    assert rec.phases == GATE_PHASES  # every phase exactly once, in order
    assert (tmp_path / "report" / "report.html").is_file()
    assert "report.html" in result["files"]
    assert result["summary"]["verdict"] == "better"
    # plus findings for the code (Ruff on the PR state)
    assert result["findings"] is not None and result["metrics"]["functions"] >= 3

    job = {"mode": "gate", "options": {"max_regression": 0.05}}
    assert gate_rules.evaluate(job, result)["status"] == "passed"


def test_gate_uses_benchmark_of_target_branch(tmp_path):
    head = optimized(tmp_path / "head")
    (head / "bench" / "run.py").write_text("print('cheated')\n", encoding="utf-8")
    ctx, _ = context(tmp_path, "gate", head, EXAMPLE)
    result = GateProcessor(settings()).run(ctx)
    assert any("'bench'" in w and "target branch" in w for w in result["warnings"])
    assert "bench" in result["shared_paths"] and "tests" not in result["shared_paths"]


def test_gate_pr_tests_fail(tmp_path):
    head = tmp_path / "head"
    shutil.copytree(EXAMPLE, head)
    core = head / "src" / "wordstats" / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8") + "\n# most_common\n", encoding="utf-8"
    )  # FakeBox: Tests rot
    ctx, _ = context(tmp_path, "gate", head, EXAMPLE)
    result = GateProcessor(settings()).run(ctx)
    assert result["measurement"] is None
    assert [t["ok"] for t in result["tests"]] == [True, False]
    verdict = gate_rules.evaluate({"mode": "gate", "options": {}}, result)
    assert verdict["status"] == "failed"
    assert verdict["conditions"][0]["name"] == "Tests (PR)"


def test_gate_target_tests_fail_is_job_error(tmp_path):
    base = tmp_path / "base"
    shutil.copytree(EXAMPLE, base)
    core = base / "src" / "wordstats" / "core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\n# most_common\n", encoding="utf-8")
    ctx, _ = context(tmp_path, "gate", optimized(tmp_path / "head"), base)
    with pytest.raises(JobError, match="target branch"):
        GateProcessor(settings()).run(ctx)


def test_gate_needs_config_in_target_branch(tmp_path):
    base = tmp_path / "base"
    shutil.copytree(EXAMPLE, base)
    (base / "joulecode.yml").unlink()
    ctx, _ = context(tmp_path, "gate", EXAMPLE, base)
    with pytest.raises(JobError, match="No joulecode.yml in the target branch"):
        GateProcessor(settings()).run(ctx)


def test_gate_backend_not_ready(tmp_path):
    class Broken(CgroupCpuBackend):
        def check(self):
            return ["no counter"]

    ctx, _ = context(tmp_path, "gate", EXAMPLE, EXAMPLE)
    with pytest.raises(JobError, match="not ready: no counter"):
        GateProcessor(settings(backend_factory=lambda s: Broken())).run(ctx)


def test_gate_cancel_during_measurement(tmp_path):
    rec = Recorder(cancel_after=12)  # aborts in the middle of the measurement chain
    ctx, _ = context(tmp_path, "gate", optimized(tmp_path / "head"), EXAMPLE, rec=rec)
    with pytest.raises(Cancelled):
        GateProcessor(settings()).run(ctx)


def test_gate_waits_for_running_cli(tmp_path, monkeypatch):
    import joulecode.cleanup as cleanup

    states = iter(["joulecode measure (PID 1)", "joulecode measure (PID 1)", None])
    monkeypatch.setattr(cleanup, "active_run", lambda: next(states))
    taken = []
    monkeypatch.setattr(cleanup, "acquire_lock", lambda cmd: taken.append(cmd))
    monkeypatch.setattr(cleanup, "release_lock", lambda: taken.append("released"))
    ctx, rec = context(tmp_path, "gate", optimized(tmp_path / "head"), EXAMPLE)
    GateProcessor(settings(use_lock=True)).run(ctx)
    assert taken == ["serve (Job job1)", "released"]
    assert any("Measurement machine busy" in msg for _, msg in rec.logs)


def test_is_test_path():
    assert is_test_path("tests") and is_test_path("src/test/java") and is_test_path("spec")
    assert not is_test_path("bench") and not is_test_path("joulecode.yml")


# -- optimize --------------------------------------------------------------------------------
def test_optimize_runs_pipeline(tmp_path):
    head = tmp_path / "repo"
    shutil.copytree(EXAMPLE, head)
    ctx, rec = context(tmp_path, "optimize", head)
    result = OptimizeProcessor(settings()).run(ctx)

    assert result["llm"] == "scripted"
    assert {h["qualname"] for h in result["hotspots"]} >= {"frequent_words", "unique_words"}
    cands = {c["summary"]: c for c in result["candidates"]}
    assert cands["most_common"]["status"] == "measured" and cands["most_common"]["origin"] == "repair"
    assert cands["Parameter lower"]["status"] == "vetoed" and cands["Parameter lower"]["change"] is None
    assert (
        cands["most_common"]["patch"] and (tmp_path / "report" / cands["most_common"]["patch_file"]).is_file()
    )
    combo = next(c for c in result["candidates"] if c["id"] == "combined")
    assert combo["status"] == "measured" and set(combo["parts"])
    assert result["measurement"]["verdict"] == "better" and result["best"] == "combined"
    assert result["summary"]["better"] >= 2
    assert "combination" in rec.phases and rec.phases[-1] == "report"
    assert (tmp_path / "report" / "report.md").is_file()


def test_optimize_llm_not_ready(tmp_path):
    class Offline(ScriptedLLM):
        name = "ollama:x"

        def check(self):
            return ["Ollama not reachable"]

    ctx, _ = context(tmp_path, "optimize", EXAMPLE)
    with pytest.raises(JobError, match="Ollama not reachable"):
        OptimizeProcessor(settings(llm_factory=lambda s: Offline())).run(ctx)


# -- via server and submit --------------------------------------------------------------------
fastapi = pytest.importorskip("fastapi")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def test_submit_gate_and_optimize_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from joulecode.server.app import ServerConfig, create_app
    from joulecode.server.review import ModeRouter, ReviewProcessor
    from joulecode.submit import EXIT_OK, cmd_submit

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    s = settings()
    router = ModeRouter(
        {"review": ReviewProcessor(), "gate": GateProcessor(s), "optimize": OptimizeProcessor(s)}
    )
    config = ServerConfig(data_dir=tmp_path / "data", admin_token="gca_t", processor=router)

    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Start")
    git(repo, "checkout", "-q", "-b", "feature")
    subprocess.run(["git", "apply", str(PATCH)], cwd=repo, check=True)
    git(repo, "commit", "-qam", "faster")

    with TestClient(create_app(config)) as client:
        token = client.post(
            "/api/projects", json={"key": "demo"}, headers={"Authorization": "Bearer gca_t"}
        ).json()["token"]
        out = io.StringIO()
        args = Namespace(
            repo=str(repo), server="http://testserver", project="demo", token=token, mode="gate",
            base="main", max_regression=5.0, fail_on="none", no_wait=False, timeout=60,
            output=None, sarif=None, all=False,
        )  # fmt: skip
        assert cmd_submit(args, client=client, out=out) == EXIT_OK, out.getvalue()
        assert "better" in out.getvalue() and "Dummy" not in out.getvalue()

        jobs = client.get("/api/projects/demo/jobs").json()
        job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        assert job["result"]["measurement"]["verdict"] == "better"
        assert job["phases"] == GATE_PHASES
        r = client.get(f"/api/jobs/{job['id']}/files/report.html")
        assert r.status_code == 200 and "sandbox" in r.headers["content-security-policy"]
        assert client.get(f"/api/jobs/{job['id']}/files/../../joulecode.db").status_code == 404
        trend = client.get("/api/projects/demo").json()["trend"]
        assert len(trend) == 1 and trend[0]["verdict"] == "better"

        git(repo, "checkout", "-q", "main")
        args.mode, args.base = "optimize", None
        out = io.StringIO()
        assert cmd_submit(args, client=client, out=out) == EXIT_OK, out.getvalue()
        job = client.get(f"/api/jobs/{client.get('/api/projects/demo/jobs').json()[0]['id']}").json()
        assert job["mode"] == "optimize" and job["status"] == "done"
        assert job["result"]["candidates"]
