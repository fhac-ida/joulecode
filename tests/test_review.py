"""Review processor: real static analysis, diff mapping, SARIF."""

from __future__ import annotations

import io
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from joulecode.analysis import fix_patch
from joulecode.server.processing import JobContext
from joulecode.server.review import ChangeSet, ModeRouter, ReviewProcessor, changed_lines, classify
from joulecode.server.sarif import to_sarif

SOURCE = """\
def double_all(xs):
    out = []
    for x in xs:
        out.append(x * 2)
    return out


def keys_sorted(d):
    return list(sorted(d.keys()))


def untouched(xs):
    res = []
    for x in xs:
        res.append(x + 1)
    return res
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "core.py").write_text(SOURCE, encoding="utf-8")
    (root / "tests").mkdir()
    # findings in tests do not count (protected path)
    (root / "tests" / "test_core.py").write_text(
        "def test_x():\n    r = []\n    for i in range(3):\n        r.append(i)\n", encoding="utf-8"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Start")
    return root


def context(
    head: Path, *, diff: str | None = None, changed: list[str] | None = None, **options
) -> JobContext:
    logs: list[tuple[str, str]] = []
    return JobContext(
        job={"id": "t", "mode": "review", "options": options, "changed_files": changed or []},
        workdir=head.parent,
        head_dir=head,
        base_dir=None,
        report_phase=lambda name, p: None,
        log=lambda level, msg: logs.append((level, msg)),
        is_cancelled=lambda: False,
        phases=ReviewProcessor().phases("review"),
        diff=diff,
    )


# -- Diff ---------------------------------------------------------------------------------
DIFF = """\
diff --git a/src/core.py b/src/core.py
--- a/src/core.py
+++ b/src/core.py
@@ -4 +4 @@ def double_all(xs):
-        out.append(x)
+        out.append(x * 2)
@@ -20,2 +19,0 @@ def gone():
-    pass
-    pass
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+def f():
+    pass
+
"""


def test_changed_lines():
    assert changed_lines(DIFF) == {"src/core.py": [(4, 4), (19, 19)], "new.py": [(1, 3)]}


def test_changeset_touches_function_spans():
    cs = ChangeSet.from_job(DIFF, ["src/core.py", "new.py", "untracked.py"])
    assert cs.touches("src/core.py", 1, 5)  # function around line 4
    assert not cs.touches("src/core.py", 12, 16)
    assert cs.touches("untracked.py", 100, 100)  # without a hunk: whole file
    assert not cs.touches("other.py", 1, 1)
    assert ChangeSet.from_job(None, []).touches("x.py", 1, 1)  # without a diff everything counts


def test_classify():
    assert classify("ruff", "PERF401")[:2] == ("major", "performance")
    assert classify("ruff", "SIM103")[:2] == ("info", "maintainability")
    assert classify("vulture", "dead-code")[1] == "dead-code"
    assert classify("eslint", "no-await-in-loop")[0] == "major"
    assert classify("unknown", "X")[:2] == ("info", "other")


def test_fix_patch_from_ruff_edits():
    lines = ["def g(d):", "    return list(sorted(d.keys()))"]
    edits = [
        {
            "content": "sorted(d.keys())",
            "location": {"row": 2, "column": 12},
            "end_location": {"row": 2, "column": 34},
        }
    ]
    assert fix_patch(lines, edits) == "-    return list(sorted(d.keys()))\n+    return sorted(d.keys())"


# -- Processor ------------------------------------------------------------------------------
def test_review_whole_repo_without_diff(tmp_path):
    repo = make_repo(tmp_path / "repo")
    result = ReviewProcessor().run(context(repo))
    assert result["dummy"] is False and result["language"] == "python"
    rules = {(f["rule"], f["function"]) for f in result["findings"]}
    assert ("PERF401", "double_all") in rules and ("PERF401", "untouched") in rules
    assert ("C413", "keys_sorted") in rules
    assert not any(f["file"].startswith("tests/") for f in result["findings"])
    assert result["summary"]["scope"] == "all"
    c413 = next(f for f in result["findings"] if f["rule"] == "C413")
    assert c413["suggestion"]["patch"].startswith("-    return list(sorted")
    assert result["metrics"]["functions"] == 3


def test_review_only_changed_functions(tmp_path):
    repo = make_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature")
    text = SOURCE.replace("out.append(x * 2)", "out.append(x * 3)")
    (repo / "src" / "core.py").write_text(text, encoding="utf-8")
    diff = git(repo, "diff", "-U0", "main")

    result = ReviewProcessor().run(context(repo, diff=diff, changed=["src/core.py"]))
    assert [(f["rule"], f["function"]) for f in result["findings"]] == [("PERF401", "double_all")]
    assert all(f["in_diff"] for f in result["findings"])
    assert result["summary"]["scope"] == "changed"
    assert result["summary"]["findings_outside_diff"] >= 2  # untouched + keys_sorted
    assert [f["function"] for f in result["changed_functions"]] == ["double_all"]

    everything = ReviewProcessor().run(context(repo, diff=diff, changed=["src/core.py"], scope="all"))
    assert len(everything["findings"]) > 1
    assert everything["findings"][0]["in_diff"]  # changed code first


def test_review_diff_without_source_files(tmp_path):
    repo = make_repo(tmp_path / "repo")
    diff = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
    result = ReviewProcessor().run(context(repo, diff=diff, changed=["README.md"]))
    assert result["findings"] == []
    assert any("no source files" in w for w in result["warnings"])


def test_mode_router():
    class P:
        def __init__(self, name):
            self.name = name

        def phases(self, mode):
            return [self.name]

    r = ModeRouter({"review": P("review"), "gate": P("dummy"), "optimize": P("dummy")})
    assert r.phases("gate") == ["dummy"] and r.describe()["review"] == "review"
    assert r.name == "dummy + review"


# -- SARIF ----------------------------------------------------------------------------------
def test_sarif(tmp_path):
    repo = make_repo(tmp_path / "repo")
    result = ReviewProcessor().run(context(repo))
    sarif = to_sarif({"id": "abc", "mode": "review", "result": result})
    run = sarif["runs"][0]
    assert sarif["version"] == "2.1.0"
    assert len(run["results"]) == len(result["findings"])
    perf = next(r for r in run["results"] if r["ruleId"] == "ruff/PERF401")
    assert perf["level"] == "error"
    loc = perf["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "src/core.py"
    assert loc["logicalLocations"][0]["fullyQualifiedName"] in ("double_all", "untouched")
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} >= {"ruff/PERF401", "ruff/C413"}


# -- via server and submit -------------------------------------------------------------------
fastapi = pytest.importorskip("fastapi")


def test_submit_review_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from joulecode.server.app import ServerConfig, create_app
    from joulecode.server.processing import DummyProcessor
    from joulecode.submit import EXIT_GATE_FAILED, cmd_submit

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    dummy = DummyProcessor(delay_s=0)
    router = ModeRouter({"review": ReviewProcessor(), "gate": dummy, "optimize": dummy})
    config = ServerConfig(data_dir=tmp_path / "data", admin_token="gca_t", processor=router)
    repo = make_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src" / "core.py").write_text(SOURCE.replace("x * 2", "x * 3"), encoding="utf-8")  # uncommittet

    with TestClient(create_app(config)) as client:
        token = client.post(
            "/api/projects", json={"key": "demo"}, headers={"Authorization": "Bearer gca_t"}
        ).json()["token"]
        assert client.get("/api/health").json()["modes"] == {
            "review": "review",
            "gate": "dummy",
            "optimize": "dummy",
        }
        sarif_path = tmp_path / "out.sarif"
        out = io.StringIO()
        args = Namespace(
            repo=str(repo), server="http://testserver", project="demo", token=token, mode="review",
            base="main", max_regression=5.0, fail_on="major", no_wait=False, timeout=30,
            output=None, sarif=str(sarif_path), all=False,
        )  # fmt: skip
        code = cmd_submit(args, client=client, out=out)
        text = out.getvalue()
        assert code == EXIT_GATE_FAILED, text  # PERF401 (major) in the changed function
        assert "double_all" in text and "untouched" not in text
        assert "more in the remaining code" in text
        sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
        assert [r["ruleId"] for r in sarif["runs"][0]["results"]] == ["ruff/PERF401"]

        job_id = client.get("/api/projects/demo/jobs").json()[0]["id"]
        r = client.get(f"/api/jobs/{job_id}/sarif")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/sarif+json")
        assert (tmp_path / "data" / "jobs" / job_id / "changes.diff").is_file()
