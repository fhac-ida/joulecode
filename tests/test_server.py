"""Platform: store, archive, worker with dummy processing, API and `joulecode submit`."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

from joulecode.server import gate
from joulecode.server.archive import ArchiveError, list_files, pack_worktree, safe_extract
from joulecode.server.processing import DummyProcessor
from joulecode.server.store import Store

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from joulecode.server.app import ServerConfig, create_app  # noqa: E402
from joulecode.submit import EXIT_ERROR, EXIT_GATE_FAILED, EXIT_OK, cmd_submit, exit_code  # noqa: E402

ADMIN = "gca_test"


# -- Helpers ------------------------------------------------------------------------
def make_repo(root: Path, *, git: bool = True) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "core.py").write_text(
        "def count_words(text):\n    out = []\n    for w in text.split():\n        out.append(w)\n    return out\n\n"
        "def unique(xs):\n    seen = []\n    for x in xs:\n        if x not in seen:\n            seen.append(x)\n    return seen\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (root / "joulecode.yml").write_text("language: python\ntest: {command: pytest}\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "ignored.txt").write_text("do not upload\n", encoding="utf-8")
    if git:
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Start")
    return root


def tar_bytes(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


@pytest.fixture
def app(tmp_path):
    config = ServerConfig(
        data_dir=tmp_path / "data",
        admin_token=ADMIN,
        processor=DummyProcessor(delay_s=0),
        start_worker=False,  # tests process jobs explicitly with worker.process_one()
    )
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def create_project(client, key="demo") -> str:
    r = client.post(
        "/api/projects", json={"key": key, "name": "Demo"}, headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


def upload(client, token, archive: bytes, mode="review", base: bytes | None = None, **form):
    files = {"archive": ("head.tar.gz", archive, "application/gzip")}
    if base is not None:
        files["base_archive"] = ("base.tar.gz", base, "application/gzip")
    return client.post(
        "/api/projects/demo/jobs",
        data={"mode": mode, **form},
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )


# -- Archive --------------------------------------------------------------------------
def test_pack_respects_gitignore_and_includes_uncommitted(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "new.py").write_text("def new():\n    pass\n", encoding="utf-8")
    files = list_files(repo)
    assert "src/core.py" in files and "new.py" in files
    assert "ignored.txt" not in files and not any(f.startswith(".git/") for f in files)

    dest = tmp_path / "out"
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(pack_worktree(repo))
    safe_extract(archive, dest)
    assert (dest / "src" / "core.py").read_text(encoding="utf-8").startswith("def count_words")


def test_pack_without_git_skips_usual_folders(tmp_path):
    repo = make_repo(tmp_path / "repo", git=False)
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "i.js").write_text("", encoding="utf-8")
    files = list_files(repo)
    assert "src/core.py" in files
    assert not any(f.startswith("node_modules") for f in files)


@pytest.mark.parametrize(
    "entry",
    [
        (tarfile.TarInfo("../evil.py"), b"x"),
        (tarfile.TarInfo("/etc/evil"), b"x"),
        (tarfile.TarInfo("a/../../evil"), b"x"),
    ],
)
def test_safe_extract_rejects_paths_outside(tmp_path, entry):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(tar_bytes([entry]))
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_safe_extract_rejects_links(tmp_path):
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(tar_bytes([(link, None)]))
    with pytest.raises(ArchiveError, match="link"):
        safe_extract(archive, tmp_path / "out")


def test_safe_extract_rejects_garbage(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"not a tar")
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path / "out")


# -- Store ------------------------------------------------------------------------------
def test_store_tokens_are_hashed_and_checked(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project, token = store.create_project("demo", "Demo")
    assert token.startswith("gcp_")
    assert token not in (tmp_path / "db.sqlite").read_bytes().decode("latin-1")
    assert store.check_project_token("demo", token)
    assert not store.check_project_token("demo", "wrong")
    new = store.rotate_token("demo")
    assert not store.check_project_token("demo", token) and store.check_project_token("demo", new)
    with pytest.raises(ValueError):
        store.create_project("demo", "duplicate")
    with pytest.raises(ValueError):
        store.create_project("With spaces", "x")


def test_store_queue_order_and_requeue(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project, _ = store.create_project("demo", "Demo")
    a = store.create_job(project, "review")
    b = store.create_job(project, "review")
    assert store.job(b)["queue_position"] == 2
    assert store.claim_next() == a
    assert store.job(b)["queue_position"] == 1
    # restart while a was running
    assert store.requeue_interrupted() == [a]
    assert store.job(a)["status"] == "queued"


# -- API + Worker -------------------------------------------------------------------------
def test_project_creation_needs_admin(client):
    assert client.post("/api/projects", json={"key": "x"}).status_code == 401
    r = client.post("/api/projects", json={"key": "x"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    create_project(client, "x")
    assert [p["key"] for p in client.get("/api/projects").json()] == ["x"]


def test_review_job_end_to_end(app, client, tmp_path):
    token = create_project(client)
    repo = make_repo(tmp_path / "repo")
    r = upload(
        client,
        token,
        pack_worktree(repo),
        branch="feature",
        commit="abc123",
        options=json.dumps({"fail_on": "major"}),
        changed_files=json.dumps(["src/core.py"]),
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["ui_url"].endswith(f"#/j/{job_id}")
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "queued"

    assert app.state.worker.process_one()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done", job
    result = job["result"]
    assert result["dummy"] is True and result["language"] == "python"
    assert result["findings"], "the dummy should generate findings for the functions in the repository"
    assert {f["function"] for f in result["findings"]} <= {"count_words", "unique"}
    assert all(f["file"] == "src/core.py" and f["in_diff"] for f in result["findings"])
    assert result["gate"]["status"] in ("passed", "failed")
    assert job["phases"] == DummyProcessor().phases("review")
    assert any("done" in log["message"] for log in job["logs"])
    # the unpacked working directory is gone, the result is stored with the data
    data = app.state.config.data_dir / "jobs" / job_id
    assert (data / "result.json").is_file() and not (data / "work").exists()


def test_gate_requires_base_and_measures(app, client, tmp_path):
    token = create_project(client)
    archive = pack_worktree(make_repo(tmp_path / "repo"))
    assert upload(client, token, archive, mode="gate").status_code == 400
    r = upload(
        client, token, archive, mode="gate", base=archive, options=json.dumps({"max_regression": 0.05})
    )
    assert r.status_code == 202
    app.state.worker.process_one()
    job = client.get(f"/api/jobs/{r.json()['id']}").json()
    m = job["result"]["measurement"]
    assert m["ci95"][0] <= m["change"] <= m["ci95"][1]
    assert job["summary"]["change"] == m["change"]
    trend = client.get("/api/projects/demo").json()["trend"]
    assert len(trend) == 1 and trend[0]["change"] == m["change"]


def test_dummy_is_deterministic_per_commit(app, client, tmp_path):
    token = create_project(client)
    archive = pack_worktree(make_repo(tmp_path / "repo"))
    ids = [upload(client, token, archive, commit="same").json()["id"] for _ in range(2)]
    while app.state.worker.process_one():
        pass
    a, b = (client.get(f"/api/jobs/{i}").json()["result"]["findings"] for i in ids)
    assert a == b


def test_submit_rejects_wrong_token_and_bad_archive(app, client):
    create_project(client)
    assert upload(client, "gcp_wrong", b"x").status_code == 401
    token = client.post("/api/projects/demo/token", headers={"Authorization": f"Bearer {ADMIN}"}).json()[
        "token"
    ]
    r = upload(client, token, b"not a tar")
    assert r.status_code == 202
    app.state.worker.process_one()
    job = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["status"] == "failed" and "Archive rejected" in job["error"]


def test_cancel_queued_job(app, client, tmp_path):
    token = create_project(client)
    job_id = upload(client, token, pack_worktree(make_repo(tmp_path / "repo"))).json()["id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 401
    r = client.post(f"/api/jobs/{job_id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["status"] == "cancelled"
    assert not app.state.worker.process_one()


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "joule:code" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json()["processor"] == "dummy"


# -- Gate rules ----------------------------------------------------------------------------
def _m(change, lo, hi, verdict):
    return {"measurement": {"change": change, "ci95": [lo, hi], "verdict": verdict}, "findings": []}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_m(0.10, 0.07, 0.13, "worse"), "failed"),  # whole CI above 5%
        (_m(0.06, 0.03, 0.09, "worse"), "passed"),  # significant, but the CI reaches below the threshold
        (_m(-0.2, -0.25, -0.15, "better"), "passed"),
        (_m(0.01, -0.01, 0.03, "no significant difference"), "passed"),
    ],
)
def test_gate_energy_threshold(result, expected):
    job = {"mode": "gate", "options": {"max_regression": 0.05}}
    assert gate.evaluate(job, result)["status"] == expected


def test_gate_findings_only_in_diff():
    findings = [
        {"severity": "major", "in_diff": False},
        {"severity": "minor", "in_diff": True},
    ]
    job = {"mode": "review", "options": {"fail_on": "major"}, "changed_files": ["a.py"]}
    assert gate.evaluate(job, {"findings": findings})["status"] == "passed"
    job["options"]["fail_on"] = "minor"
    assert gate.evaluate(job, {"findings": findings})["status"] == "failed"
    assert gate.evaluate({"mode": "review", "options": {}}, {"findings": findings})["status"] == "skipped"


# -- joulecode submit --------------------------------------------------------------------
def _submit_args(repo: Path, **kw) -> Namespace:
    defaults = dict(
        repo=str(repo),
        server="http://testserver",
        project="demo",
        token=None,
        mode="review",
        base=None,
        max_regression=5.0,
        fail_on="none",
        no_wait=False,
        timeout=5,
        output=None,
    )
    defaults.update(kw)
    return Namespace(**defaults)


@pytest.fixture
def running_client(tmp_path):
    """TestClient with a running worker – submit waits for the result as in CI."""
    config = ServerConfig(data_dir=tmp_path / "data", admin_token=ADMIN, processor=DummyProcessor(delay_s=0))
    with TestClient(create_app(config)) as c:
        yield c


def test_submit_review_waits_and_writes_output(running_client, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    token = create_project(running_client)
    repo = make_repo(tmp_path / "repo")
    out_file = tmp_path / "result.json"
    out = io.StringIO()
    code = cmd_submit(_submit_args(repo, token=token, output=str(out_file)), client=running_client, out=out)
    assert code == EXIT_OK, out.getvalue()
    job = json.loads(out_file.read_text(encoding="utf-8"))
    assert job["status"] == "done" and job["branch"] == "main" and job["commit_sha"]
    assert "dummy result" in out.getvalue()


def test_submit_gate_uses_merge_base_and_changed_files(running_client, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    token = create_project(running_client)
    repo = make_repo(tmp_path / "repo")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("checkout", "-q", "-b", "feature")
    (repo / "src" / "core.py").write_text("def faster(x):\n    return set(x)\n", encoding="utf-8")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "faster")

    out = io.StringIO()
    code = cmd_submit(
        _submit_args(repo, token=token, mode="gate", base="main"), client=running_client, out=out
    )
    assert code in (EXIT_OK, EXIT_GATE_FAILED), out.getvalue()
    jobs = running_client.get("/api/projects/demo/jobs").json()
    job = running_client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert job["branch"] == "feature" and job["base_ref"] == "main" and job["has_base"]
    assert job["changed_files"] == ["src/core.py"]
    assert job["options"]["max_regression"] == pytest.approx(0.05)
    assert "joule:code gate" in summary.read_text(encoding="utf-8")


def test_submit_errors(running_client, tmp_path):
    repo = make_repo(tmp_path / "repo")
    err = io.StringIO()
    assert cmd_submit(_submit_args(repo, token=None), client=running_client, out=err) == EXIT_ERROR
    assert cmd_submit(_submit_args(repo, token="gcp_wrong"), client=running_client, out=err) == EXIT_ERROR
    token = create_project(running_client)
    # gate without --base
    assert (
        cmd_submit(_submit_args(repo, token=token, mode="gate"), client=running_client, out=err) == EXIT_ERROR
    )


def test_exit_codes():
    assert exit_code({"status": "failed"}) == EXIT_ERROR
    assert exit_code({"status": "done", "result": {"gate": {"status": "failed"}}}) == EXIT_GATE_FAILED
    assert exit_code({"status": "done", "result": {"gate": {"status": "skipped"}}}) == EXIT_OK
