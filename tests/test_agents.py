from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from joulecode.agents import Candidate, Documenter, Reviewer, ReviewSettings, materialize
from joulecode.analysis import analyze_repo, complexity_of
from joulecode.config import load_config
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.lang import get_language
from joulecode.optimize import Pipeline
from joulecode.optimize_report import write_optimize_reports
from joulecode.profiling import profile_command, select_hotspots
from joulecode.pycode import add_imports, find_function, function_at_line, insert_docstring, replace_function
from tests.fakes import API_BREAK_UNIQUE, OPT_FREQUENT, PathRunner, ScriptedLLM

EXAMPLE = Path(__file__).parent.parent / "examples" / "python-wordstats"

CLASS_SRC = '''"""Module."""
import os


class Parser:
    @staticmethod
    def parse(x):
        """Old."""
        return x + 1

    def other(self):
        return 2
'''


# -- pycode -------------------------------------------------------------------
def test_replace_method_keeps_class_indentation():
    new = replace_function(CLASS_SRC, "Parser.parse", "def parse(x):\n    return x + 2\n")
    assert "    def parse(x):\n        return x + 2\n" in new
    assert "@staticmethod" not in new  # decorators belong to the replaced function
    assert "def other(self)" in new


def test_replace_rejects_wrong_name_and_extra_code():
    with pytest.raises(ValueError):
        replace_function(CLASS_SRC, "Parser.parse", "def wrong(x):\n    return x\n")
    with pytest.raises(ValueError):
        replace_function(CLASS_SRC, "Parser.parse", "def parse(x):\n    return x\nprint(1)\n")


def test_add_imports_after_existing_and_dedup():
    new = add_imports(CLASS_SRC, ["import os", "from collections import Counter"])
    assert new.count("import os") == 1
    assert new.index("from collections import Counter") < new.index("class Parser")


def test_add_imports_after_docstring_only():
    new = add_imports('"""Doc."""\n\nX = 1\n', ["import re"])
    assert new.startswith('"""Doc."""\n\nimport re\n')


def test_insert_and_replace_docstring():
    src = "def f(a):\n    return a\n"
    with_doc = insert_docstring(src, "f", "Returns a.")
    assert with_doc == 'def f(a):\n    """Returns a."""\n    return a\n'
    replaced = insert_docstring(with_doc, "f", "Line 1\n\nLine 2")
    assert '"""Line 1\n\n    Line 2\n    """' in replaced


def test_function_at_line_matches_decorator_line():
    assert function_at_line(CLASS_SRC, 6).qualname == "Parser.parse"
    assert function_at_line(CLASS_SRC, 7).qualname == "Parser.parse"


# -- Profiling ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("python bench/run.py --n 3", "python -m cProfile -o /tmp/p bench/run.py --n 3"),
        ("python3 -u -m bench", "python3 -u -m cProfile -o /tmp/p -m bench"),
    ],
)
def test_profile_command(cmd, expected):
    assert profile_command(cmd, "/tmp/p") == expected


def test_profile_command_rejects_shell():
    with pytest.raises(ValueError):
        profile_command("./run.sh")


def test_select_hotspots_maps_lines_to_functions():
    src = (EXAMPLE / "src/wordstats/core.py").read_text()
    line = find_function(src, "frequent_words").def_line
    stats = {
        "total_s": 10,
        "functions": [
            {
                "file": "src/wordstats/core.py",
                "line": line,
                "function": "frequent_words",
                "ncalls": 1,
                "tottime": 8,
                "cumtime": 9,
            },
            {
                "file": "src/wordstats/core.py",
                "line": 1,
                "function": "<module>",
                "ncalls": 1,
                "tottime": 1,
                "cumtime": 10,
            },
        ],
    }
    (h,) = select_hotspots(stats, EXAMPLE, limit=3, min_share=0.05)
    assert h.qualname == "frequent_words" and h.share == pytest.approx(0.8)
    assert h.code.startswith("def frequent_words")


# -- Analyse -------------------------------------------------------------------
def test_static_analysis_of_example():
    a = analyze_repo(EXAMPLE, ["tests", "bench", "joulecode.yml"])
    assert a.metrics.files == 2
    assert a.function("src/wordstats/core.py", "frequent_words").complexity == 4
    assert complexity_of("def f(x):\n    return 1 if x else 2\n") == 2


# -- Reviewer / documenter -------------------------------------------------------
def _candidate(tmp_path, functions, imports=()) -> Candidate:
    c = Candidate("c1", "src/wordstats/core.py", "x", "t", "r", list(imports), functions)
    materialize(c, EXAMPLE, tmp_path / "c1")
    return c


def test_unchanged_function_is_dropped_from_candidate(tmp_path):
    # The LLM returns the hotspot unchanged and optimises another function.
    import re

    source = (EXAMPLE / "src/wordstats/core.py").read_text(encoding="utf-8")
    original = get_language().find_function(source, "frequent_words")
    unchanged = "\n".join(source.splitlines()[original.start - 1 : original.end])
    better = (
        "def unique_words(text: str) -> list[str]:\n    return list(dict.fromkeys(text.lower().split()))\n"
    )
    c = _candidate(
        tmp_path, [{"name": "unique_words", "code": better}, {"name": "frequent_words", "code": unchanged}]
    )
    assert c.changed == ["unique_words"]
    assert re.search(r"^[-+]def frequent_words", c.patch, re.M) is None


def test_reviewer_vetoes_signature_change(tmp_path):
    c = _candidate(tmp_path, [{"name": "unique_words", "code": API_BREAK_UNIQUE}])
    review = Reviewer(None, ReviewSettings()).review(c, EXAMPLE)
    assert review["veto"] and "Signature" in review["rule_vetoes"][0]


def test_reviewer_vetoes_new_dependency(tmp_path):
    code = "def unique_words(text: str) -> list[str]:\n    return list(numpy.unique(text.split()))\n"
    c = _candidate(tmp_path, [{"name": "unique_words", "code": code}], ["import numpy"])
    review = Reviewer(None, ReviewSettings()).review(c, EXAMPLE)
    assert any("numpy" in v for v in review["rule_vetoes"])


def test_reviewer_metrics_without_llm(tmp_path):
    c = _candidate(
        tmp_path, [{"name": "frequent_words", "code": OPT_FREQUENT}], ["from collections import Counter"]
    )
    review = Reviewer(None, ReviewSettings()).review(c, EXAMPLE)
    fn = review["metrics"]["functions"][0]
    assert not review["veto"]
    assert fn["complexity_after"] < fn["complexity_before"]
    assert review["score"] > 3  # less complexity → better score


def test_documenter_restores_original_docstring(tmp_path):
    c = _candidate(
        tmp_path, [{"name": "frequent_words", "code": OPT_FREQUENT}], ["from collections import Counter"]
    )
    actions = Documenter(None).ensure_docstrings(c, EXAMPLE)
    assert "restored" in actions[0]
    src = (c.path / c.file).read_text()
    assert "ties sorted alphabetically" in find_function(src, "frequent_words").source(src.splitlines(True))
    assert '"""The `top` most frequent' in c.patch or "ties sorted" in src


# -- Pipeline -------------------------------------------------------------------
def test_pipeline_end_to_end_with_fakes(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    llm = ScriptedLLM()
    pipe = Pipeline(cfg, PathRunner(), CgroupCpuBackend(), llm, log=lambda _: None, wait_gpu=lambda: True)
    result = pipe.run(repo, tmp_path / "work")

    assert result.status == "ok", result.error
    by_title = {c.title: c for c in result.candidates}
    # candidate with wrong tie-breaking: tests red → repaired → admitted
    assert by_title["most_common"].origin == "repair"
    assert by_title["most_common"].status == "measured"
    # after the repair of the first one, the second candidate has the same code → duplicate, not measured
    assert by_title["Counter + sorted"].status == "duplicate"
    assert by_title["most_common"].id in by_title["Counter + sorted"].messages[-1]
    # signature change: veto → revision round with feedback → new candidate
    assert by_title["Parameter lower"].status == "vetoed"
    assert by_title["dict.fromkeys"].origin == "revision"
    assert any("rejected" in p for role, p in llm.calls if role == "optimizer")
    # unload the LLM before the measurement
    assert llm.unloaded
    measured = [c for c in result.candidates if c.status == "measured"]
    assert {r["id"] for r in result.ranking} == {c.id for c in measured} | {"combined"}
    # Pareto per hotspot: both hotspots have a Pareto candidate
    assert {r["hotspot"] for r in result.ranking if r["pareto"]} >= {"frequent_words", "unique_words"}
    # combination of the best candidates of both hotspots: tested and measured
    combo = result.combination
    assert combo.status == "measured"
    assert set(combo.parts) == {by_title["most_common"].id, by_title["dict.fromkeys"].id}
    assert "Counter" in (combo.path / "src/wordstats/core.py").read_text()
    assert "dict.fromkeys" in (combo.path / "src/wordstats/core.py").read_text()

    report = write_optimize_reports(result, tmp_path / "out")
    text = report.read_text()
    assert "Pareto" in text and "★" in text and "## Combination" in text
    assert (tmp_path / "out" / "measurement-combined" / "report.md").exists()
    cand_report = (tmp_path / "out" / "candidates" / f"{by_title['most_common'].id}.md").read_text()
    assert "Measured effect" in cand_report and "docstring" in cand_report
    assert "duplicate" in text


def test_pipeline_stops_when_original_tests_fail(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    core = repo / "src/wordstats/core.py"
    core.write_text(core.read_text().replace("seen = []", "seen = []  # BROKEN"))
    pipe = Pipeline(load_config(repo), PathRunner(), CgroupCpuBackend(), ScriptedLLM(), log=lambda _: None)
    result = pipe.run(repo, tmp_path / "work")
    assert result.status == "baseline_tests_failed"
    assert result.candidates == []


def test_rank_ties_within_noise_are_both_pareto():
    from joulecode.optimize import _dominates

    a = {"rel_change": -0.87, "ci_low": -0.88, "ci_high": -0.82, "maintainability": 4.0}
    b = {"rel_change": -0.86, "ci_low": -0.88, "ci_high": -0.84, "maintainability": 4.0}
    c = {"rel_change": -0.20, "ci_low": -0.25, "ci_high": -0.10, "maintainability": 4.5}
    assert not _dominates(a, b) and not _dominates(b, a)  # CIs overlap, same score
    assert not _dominates(a, c)  # more efficient, but less maintainable
    d = dict(b, maintainability=3.0)
    assert _dominates(a, d)  # equally efficient, more maintainable


def test_checkpoint_writes_real_reports_before_evaluation(tmp_path):
    """Regression: the intermediate state before the analysis must be writable with the real report functions."""
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 2
    cfg.measurement.cooldown_s = 0
    cfg.optimize.combine = False
    out = tmp_path / "out"
    seen = []

    def checkpoint(result):
        report = write_optimize_reports(result, out)
        seen.append([r["note"] for r in result.measurement.runs])
        assert report.exists() and (out / "measurement" / "report.md").exists()

    pipe = Pipeline(
        cfg, PathRunner(), CgroupCpuBackend(), ScriptedLLM(), log=lambda _: None, wait_gpu=lambda: True,
        on_checkpoint=checkpoint,
    )  # fmt: skip
    result = pipe.run(repo, tmp_path / "work")
    assert result.status == "ok"
    assert seen and all(n == "not analysed yet" for n in seen[0])


def test_failing_checkpoint_does_not_abort_measurement(tmp_path):
    from joulecode.config import ProjectConfig
    from joulecode.protocol import measure
    from joulecode.variants import Variant
    from tests.test_core import FakeRunner

    cfg = ProjectConfig.model_validate(
        {
            "test": {"command": "pytest"},
            "benchmark": {"command": "bench", "min_duration_s": 0},
            "measurement": {"repetitions": 2, "cooldown_s": 0},
        }
    )
    (tmp_path / "b").mkdir()
    logs = []

    def broken(_):
        raise KeyError("value")

    result = measure(
        cfg, [Variant("baseline", tmp_path / "b")], FakeRunner({"baseline": 1.0}), CgroupCpuBackend(),
        log=logs.append, checkpoint=broken,
    )  # fmt: skip
    assert result.status == "ok"
    assert all(r["value"] is not None for r in result.runs)
    assert any("intermediate state could not be saved" in x for x in logs)


def test_resolve_name_without_class():
    from joulecode.lang import get_language

    lang = get_language("python")
    assert lang.resolve_name(CLASS_SRC, "parse") == "Parser.parse"
    assert lang.resolve_name(CLASS_SRC, "Parser.parse") == "Parser.parse"
    assert lang.resolve_name(CLASS_SRC, "missing") == "missing"


def test_same_code_ignores_whitespace_and_class_prefix():
    from joulecode.optimize import _same_code

    a = [{"name": "Parser.parse", "code": "def parse(x):\n    return x"}]
    b = [{"name": "parse", "code": "def parse(x):\n        return x\n"}]
    assert _same_code(a, b)
    assert not _same_code(a, [{"name": "parse", "code": "def parse(x):\n    return x + 1"}])
    # just one more import is also a change (missing import → compiler error)
    assert not _same_code(a, b, [], ["import java.util.HashSet;"])


def test_python_canonical_ignores_formatting_but_not_indentation():
    from joulecode.lang import get_language

    lang = get_language("python")
    a = "def f(x):\n    return [y for y in x if y]  # comment\n"
    b = "def f( x ):\n    return [\n        y for y in x\n        if y\n    ]\n"
    assert lang.canonical(a) == lang.canonical(b)
    loop_a = "def g(xs):\n    for x in xs:\n        print(x)\n        return x\n"
    loop_b = "def g(xs):\n    for x in xs:\n        print(x)\n    return x\n"
    assert lang.canonical(loop_a) != lang.canonical(loop_b)


def _cand(cid: str, hotspot: str, names: list[str]) -> Candidate:
    return Candidate(cid, "src/a.js", hotspot, cid, "", [], [{"name": n, "code": ""} for n in names])


def _row(cid: str, hotspot: str, rel: float) -> dict:
    return {"id": cid, "hotspot": hotspot, "rel_change": rel, "verdict": "better", "pareto": True}


def test_select_combination_prefers_candidate_covering_several_hotspots():
    from joulecode.optimize import select_combination

    by_id = {
        "c1": _cand("c1", "A", ["A"]),
        "c2": _cand("c2", "A + B", ["A", "B"]),
        "c3": _cand("c3", "B", ["B"]),
    }
    rows = [_row("c1", "A", -0.80), _row("c2", "A + B", -0.97), _row("c3", "B", -0.34)]
    chosen, covered = select_combination(rows, by_id)
    assert [c.id for c in chosen] == ["c2"] and covered is by_id["c2"]
    # without the candidate for both hotspots: classic combination of the single ones
    chosen, covered = select_combination([rows[0], rows[2]], by_id)
    assert [c.id for c in chosen] == ["c1", "c3"] and covered is None
