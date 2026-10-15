"""HTML reports: content, well-formedness, special cases and before/after diff."""

from __future__ import annotations

import json
import shutil
from html.parser import HTMLParser
from pathlib import Path

from joulecode import html_report as H
from joulecode.config import load_config
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.optimize import OptimizeResult, Pipeline
from joulecode.optimize_report import write_optimize_reports
from joulecode.protocol import GateResult, MeasurementResult, load_result
from tests.fakes import PathRunner, ScriptedLLM

EXAMPLE = Path(__file__).parent.parent / "examples" / "python-wordstats"
VOID = {"meta", "link", "br", "img", "input", "col", "hr", "source", "wbr"}
SVG_VOID = {"line", "circle", "rect", "path"}


class _Checker(HTMLParser):
    """Checks that every opened element is closed again."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.external: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value and value.startswith(("http:", "https:", "//")):
                self.external.append(value)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> does not match {self.stack[-3:]}")
            return
        self.stack.pop()


def check_html(text: str) -> None:
    c = _Checker()
    c.feed(text)
    assert not c.errors, c.errors[:5]
    assert c.stack == [], c.stack
    assert not c.external, "report must not load anything from elsewhere"
    assert "<script src" not in text


def _optimize(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    pipe = Pipeline(
        cfg, PathRunner(), CgroupCpuBackend(), ScriptedLLM(), log=lambda _: None, wait_gpu=lambda: True
    )
    return pipe.run(repo, tmp_path / "work")


def test_optimize_html_written_with_all_sections(tmp_path):
    result = _optimize(tmp_path)
    write_optimize_reports(result, tmp_path / "out")
    text = (tmp_path / "out" / "report.html").read_text(encoding="utf-8")
    check_html(text)
    for part in ("Optimisation report", "Best single candidate", "Energy versus maintainability", "Before / after",
                 "Individual measurements", "Second measurement: combination", "Hotspots &amp; static analysis"):  # fmt: skip
        assert part in text, part
    # every candidate has an expandable card, the measured ones with a diff
    for c in result.candidates:
        assert f'id="cand-{c.id}"' in text
    assert text.count('class="diff"') >= len([c for c in result.candidates if c.patch])
    assert "CPU time as a proxy" in text  # the cgroup back-end is marked as such
    # measurement reports of the campaigns as HTML as well
    for sub in ("measurement", "measurement-combined"):
        sub_text = (tmp_path / "out" / sub / "report.html").read_text(encoding="utf-8")
        check_html(sub_text)
        assert "Measurement report" in sub_text


def test_optimize_html_without_hotspots_and_with_error():
    result = OptimizeResult(
        status="no_hotspots", started_at="2026-10-07T10:00:00", error="No function above the threshold"
    )
    text = H.render_optimize_html(result)
    check_html(text)
    assert "Profiling found no function" in text
    assert "No function above the threshold" in text


def test_measurement_html_gate_failure_without_runs():
    r = MeasurementResult(
        status="gate_failed",
        started_at="2026-10-07T10:00:00",
        gates=[
            GateResult("baseline", True, 0, 1.2, ""),
            GateResult("candidate", False, 1, 0.8, "FAILED test_x <b>"),
        ],
        error="The candidate's tests failed",
    )
    text = H.render_measurement_html(r)
    check_html(text)
    assert "failed (exit 1)" in text
    assert "FAILED test_x &lt;b&gt;" in text  # output is escaped


def test_measurement_html_marks_aa_test(tmp_path):
    result = _optimize(tmp_path)
    data = json.loads(json.dumps(result.measurement.__dict__, default=lambda o: o.__dict__))
    m = load_result(data)
    m.variants[0]["notes"] = ["A/A control: identical to baseline"]
    text = H.render_measurement_html(m)
    check_html(text)
    assert "A/A noise test" in text


PATCH = """--- a/src/x.py
+++ b/src/x.py
@@ -1,5 +1,4 @@
 def f(items):
-    seen = []
-    for i in items:
-        seen.append(i)
-    return seen
+    seen = list(items)
+    return seen
"""


def test_parse_patch_line_numbers_and_pairs():
    (f,) = H.parse_patch(PATCH)
    assert f["file"] == "src/x.py"
    (hunk,) = f["hunks"]
    ops = [(op, o, n) for op, o, n, _ in hunk["lines"]]
    assert ops[0] == (" ", 1, 1)
    assert ops[1:5] == [("-", 2, None), ("-", 3, None), ("-", 4, None), ("-", 5, None)]
    assert ops[5:] == [("+", None, 2), ("+", None, 3)]
    rows = H._pair_blocks(hunk["lines"])
    # 4 deleted, 2 new lines → 4 pairs of lines, the last two empty on the right
    chg = [r for r in rows if r[0] == "chg"]
    assert len(chg) == 4 and chg[2][2] is None and chg[3][2] is None


def test_diff_html_escapes_and_highlights_words():
    patch = '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = "<a>" + y\n+x = "<a>" + z\n'
    out = H.diff_html(patch)
    assert "&lt;a&gt;" in out and "<a>" not in out
    assert "<mark>y</mark>" in out and "<mark>z</mark>" in out
    assert H.diff_html("") == ""


def test_number_formatting_is_english():
    assert H.pct(-0.514) == "−51.4%"
    assert H.pct(0.0001) == "0%"
    assert H.pct(0.25, 0) == "+25%"
    assert H.num(1234.5) == "1,234"
    assert H.num(0.000181) == "0.000181"
    assert H.num(None) == "–"
    assert H.score(4.0) == "4" and H.score(3.5) == "3.5"


def test_report_command_regenerates_from_result_json(tmp_path):
    from joulecode.cli import main

    result = _optimize(tmp_path)
    out = tmp_path / "out"
    write_optimize_reports(result, out)
    for f in ("report.html", "report.md", "measurement/report.html"):
        (out / f).unlink()
    assert main(["report", str(out)]) == 0
    assert "Optimisation report" in (out / "report.html").read_text(encoding="utf-8")
    assert (out / "measurement" / "report.html").is_file()
    (out / "measurement" / "report.html").unlink()
    assert main(["report", str(out / "measurement")]) == 0
    check_html((out / "measurement" / "report.html").read_text(encoding="utf-8"))
    assert main(["report", str(tmp_path)]) == 2
