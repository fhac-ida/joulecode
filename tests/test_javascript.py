"""JavaScript/TypeScript back-end: code operations, analysis, CPU profile analysis, pipeline with fakes.

Most tests need `node` and the tool packages (typescript, eslint, …):
JOULECODE_JS_NODE_MODULES=<path/to/node_modules> pytest tests/test_javascript.py
Without this variable they are skipped (the pure Python parts always run).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from joulecode import prompts
from joulecode.agents import Candidate, Documenter, Reviewer, ReviewSettings, materialize
from joulecode.config import load_config
from joulecode.container import ExecResult
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.lang import get_language
from joulecode.lang import javascript as js_backend
from joulecode.lang.javascript import JavaScriptLanguage, JsTools, map_v8_name, package_names, parse_eslint
from joulecode.optimize import Pipeline
from joulecode.optimize_report import write_optimize_reports

EXAMPLE = Path(__file__).parent.parent / "examples" / "js-wordstats"
SRC = "src/wordstats.js"
HAS_NODE = shutil.which("node") is not None
HAS_TOOLS = HAS_NODE and bool(os.environ.get("JOULECODE_JS_NODE_MODULES"))
needs_node = pytest.mark.skipif(not HAS_NODE, reason="needs node")
needs_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="needs node and JOULECODE_JS_NODE_MODULES (typescript)"
)


@pytest.fixture
def lang(monkeypatch) -> JavaScriptLanguage:
    monkeypatch.setenv("JOULECODE_JS_TOOLS", "local")
    monkeypatch.setattr(js_backend, "_TOOLS", None)
    return JavaScriptLanguage(JsTools(mode="local"))


TS_SRC = """import { readFileSync } from 'node:fs';
import { helper } from './util.js';

/**
 * Word statistics.
 */
export class Stats {
  /**
   * Old.
   */
  static parse(x: number): number {
    return x + 1;
  }

  private other = (): number => 2;
}

export const twice = (x: number): number => x * 2;
"""


# -- pure Python parts ------------------------------------------------------------------
def test_get_language_variants():
    assert get_language("javascript").prompts.fence == "js"
    ts = get_language("typescript")
    assert ts.name == "javascript" and ts.prompts.fence == "ts" and ts.prompts.language == "TypeScript"


def test_parse_eslint_splits_dead_code():
    out = json.dumps(
        [
            {
                "filePath": "/repo/src/a.js",
                "messages": [
                    {"ruleId": "unicorn/prefer-set-has", "message": "Use Set#has().", "line": 4},
                    {"ruleId": "no-unused-vars", "message": "'x' is unused.", "line": 9},
                ],
            }
        ]
    )
    hint, dead = parse_eslint("Warning\n" + out, "/repo")
    assert (hint.tool, hint.code, hint.file, hint.line) == ("eslint", "unicorn/prefer-set-has", "src/a.js", 4)
    assert dead.tool == "eslint-dead"


def test_map_v8_name_and_package_names(tmp_path):
    assert map_v8_name("get size") == "size" and map_v8_name("uniqueWords") == "uniqueWords"
    (tmp_path / "package.json").write_text(json.dumps({"name": "app", "dependencies": {"lodash": "^4"}}))
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text(json.dumps({"name": "foreign"}))
    assert package_names(tmp_path) == ["app", "lodash"]


def test_prompt_mentions_sampling_profiler():
    lp = get_language("javascript").prompts
    text = prompts.render(
        prompts.OPTIMIZER_PROMPT, lp, qualname="uniqueWords", file=SRC, share=0.5,
        calls="call count unknown (sampling profiler)", tottime=1.0, cumtime=1.0, complexity=3,
        hints="- none", code="x", context="x", feedback="", n=2,
    )  # fmt: skip
    assert "```js" in text and "sampling profiler" in text


# -- CPU profile analysis (needs node only) ---------------------------------------------
def _cpuprofile(app: Path) -> dict:
    src, bench = f"file://{app}/src/wordstats.js", f"file://{app}/bench/run.js"

    def node(i, fn, url, line, children=()):
        return {"id": i, "callFrame": {"functionName": fn, "url": url, "lineNumber": line, "columnNumber": 0},
                "children": list(children)}  # fmt: skip

    return {
        "nodes": [
            node(1, "(root)", "", -1, [2, 6]),
            node(2, "", bench, 0, [3]),
            node(3, "uniqueWords", src, 18, [4]),
            node(4, "includes", "", -1, []),
            node(6, "(garbage collector)", "", -1, []),
        ],
        # samples: 2× in includes (counts towards uniqueWords), 1× uniqueWords, 1× benchmark, 1× GC
        "samples": [4, 4, 3, 2, 6],
        "timeDeltas": [0, 1000, 1000, 1000, 1000],
    }


@needs_node
def test_cpuprofile_attributes_builtins_to_caller(tmp_path):
    app = tmp_path / "app"
    (app / "prof").mkdir(parents=True)
    (app / "prof" / "CPU.1.cpuprofile").write_text(json.dumps(_cpuprofile(app)))
    script = (js_backend.TOOLS_DIR / "cpuprofile.js").read_text()
    res = subprocess.run(
        ["node", "-e", script, str(app / "prof"), json.dumps(["bench"])],
        cwd=app, capture_output=True, text=True, check=True,
    )  # fmt: skip
    stats = json.loads(res.stdout.split("JOULECODE_JSON", 1)[1])
    assert stats["total_s"] == pytest.approx(
        0.005
    )  # 5 samples of 1 ms each (last one: distance to the previous one)
    (row,) = stats["functions"]  # bench/ excluded
    assert (row["file"], row["function"], row["line"]) == (SRC, "uniqueWords", 19)
    assert row["tottime"] == pytest.approx(0.003) and row["cumtime"] == pytest.approx(0.003)


# -- Code operations (TypeScript compiler API) ------------------------------------------
@needs_tools
def test_functions_spans_signature_doc_complexity(lang):
    parse, other, twice = lang.functions(TS_SRC)
    assert (parse.qualname, parse.start, parse.def_line, parse.end) == ("Stats.parse", 8, 11, 13)
    assert parse.indent == "  " and parse.docstring == "Old."
    assert parse.signature == "parse(x:number):number[static]"
    assert other.qualname == "Stats.other" and twice.qualname == "twice"
    assert twice.signature == "twice(x:number):number[export]"
    src = (EXAMPLE / SRC).read_text()
    assert lang.complexity(src, "frequentWords") == 7  # 1 + 2 loops + if + 2× ?: + ||
    assert lang.complexity(src, "uniqueWords") == 3


@needs_tools
def test_replace_method_keeps_class_indentation(lang):
    new = lang.replace_function(
        TS_SRC, "Stats.parse", "static parse(x: number): number {\n  return x + 2;\n}"
    )
    assert "  static parse(x: number): number {\n    return x + 2;\n  }\n" in new
    assert "Old." not in new  # JSDoc belongs to the replaced function
    assert "private other" in new


@needs_tools
def test_replace_const_arrow_and_errors(lang):
    new = lang.replace_function(TS_SRC, "twice", "export const twice = (x: number): number => x + x;")
    assert new.rstrip().endswith("export const twice = (x: number): number => x + x;")
    with pytest.raises(ValueError, match="expected"):
        lang.replace_function(TS_SRC, "twice", "export const thrice = (x: number): number => x * 3;")
    with pytest.raises(ValueError):
        lang.replace_function(TS_SRC, "twice", "export const twice = (x) => x;\nconsole.log(1);")
    with pytest.raises(ValueError, match="Syntax"):
        lang.replace_function(TS_SRC, "Stats.parse", "static parse(x: number) { return x + }")


@needs_tools
def test_add_imports_after_existing_and_at_top(lang):
    new = lang.add_imports(
        TS_SRC,
        lang.normalize_imports(["import { readFileSync } from 'node:fs'", "import os from 'node:os'"], ""),
    )
    assert new.count("readFileSync") == 1
    assert "import { helper } from './util.js';\nimport os from 'node:os';\n" in new
    top = lang.add_imports((EXAMPLE / SRC).read_text(), ["import { strict } from 'node:assert';"])
    assert top.startswith("import { strict } from 'node:assert';\n\n/**")
    with pytest.raises(ValueError):
        lang.add_imports(TS_SRC, ["console.log(1);"])


@needs_tools
def test_insert_and_replace_jsdoc(lang):
    src = "export function f(a) {\n  return a;\n}\n"
    with_doc = lang.insert_docstring(src, "f", "Returns a.")
    assert with_doc.startswith("/**\n * Returns a.\n */\nexport function f(a)")
    replaced = lang.insert_docstring(with_doc, "f", "Line 1\n\n@param {number} a")
    assert lang.find_function(replaced, "f").docstring == "Line 1\n\n@param {number} a"


@needs_tools
def test_new_dependencies_ignore_node_builtins_and_package_json(lang):
    after = lang.add_imports(
        TS_SRC, ["import os from 'node:os';", "import _ from 'lodash/fp';", "import x from 'leftpad';"]
    )
    new, third = lang.new_dependencies(TS_SRC, after, frozenset({"lodash"}))
    assert new == ["leftpad", "lodash", "node:os"]
    assert third == ["leftpad"]


@needs_tools
def test_static_analysis_of_example(lang):
    a = lang.analyze_repo(EXAMPLE, load_config(EXAMPLE).protected_paths)
    assert a.metrics.files == 1 and a.metrics.language == "JavaScript/TypeScript"
    assert {f.qualname for f in a.functions} == {"words", "uniqueWords", "frequentWords", "report"}
    assert 0 < a.metrics.mi_avg <= 100
    assert "js-wordstats" in lang.internal_modules(a)


# -- Agents and pipeline with fakes ---------------------------------------------------------
# Object keys move integer words to the front → test “numeric words stay in place” fails.
OPT_UNIQUE_WRONG = """export function uniqueWords(text) {
  const seen = {};
  for (const word of words(text)) seen[word] = true;
  return Object.keys(seen);
}"""

OPT_UNIQUE = """/**
 * All words in order of their first occurrence, lower-cased.
 *
 * @param {string} text
 * @returns {string[]}
 */
export function uniqueWords(text) {
  return [...new Set(words(text))];
}"""

OPT_FREQUENT = """export function frequentWords(text, top = 10) {
  const counts = new Map();
  for (const word of words(text)) {
    counts.set(word, (counts.get(word) ?? 0) + 1);
  }
  const pairs = [...counts];
  pairs.sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return pairs.slice(0, top);
}"""

API_BREAK = OPT_FREQUENT.replace("(text, top = 10)", "(text, top = 10, sorted = true)")


def _proposal(title: str, code: str) -> dict:
    name = code.split("function ", 1)[1].split("(", 1)[0]
    return {"title": title, "rationale": f"{title}: hash instead of linear search.", "imports": [],
            "functions": [{"name": name, "code": code}]}  # fmt: skip


class JsScriptedLLM:
    name = "scripted-js"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.systems: list[str] = []
        self.unloaded = False

    def generate_json(self, system, prompt, schema, *, max_tokens=None):
        self.systems.append(system)
        if schema is prompts.OPTIMIZER_SCHEMA:
            self.calls.append(("optimizer", prompt))
            head = prompt.split("\n", 1)[0]
            if "uniqueWords" in head:
                if "did NOT pass the tests" in prompt:
                    return {"candidates": [_proposal("Set (repaired)", OPT_UNIQUE)]}
                return {"candidates": [_proposal("object keys", OPT_UNIQUE_WRONG)]}
            if "frequentWords" in head:
                if "rejected" in prompt:
                    return {"candidates": [_proposal("counting with Map", OPT_FREQUENT)]}
                return {"candidates": [_proposal("new parameter", API_BREAK)]}
            return {"candidates": []}
        if schema is prompts.REVIEWER_SCHEMA:
            self.calls.append(("reviewer", prompt))
            return {"maintainability": 4, "concerns": [], "suggestion": "", "veto": False}
        if schema is prompts.DOC_SCHEMA:
            self.calls.append(("doc", prompt))
            return {"docstring": "Most frequent words."}
        raise AssertionError("unknown schema")

    def unload(self) -> None:
        self.unloaded = True


class JsBox:
    def __init__(self, path: Path) -> None:
        self.path, self.id, self.cpu = path, "box", 0.0

    def exec(self, command, timeout_s, env=None):
        if command.startswith("node --test"):
            res = subprocess.run(
                ["sh", "-c", command],
                cwd=self.path,
                capture_output=True,
                text=True,
                env={**os.environ, "NODE_OPTIONS": ""},
            )
            return ExecResult(res.returncode, res.stdout + res.stderr, 0.1)
        if command.startswith("node -e"):
            assert env == {"NODE_OPTIONS": ""}
            stats = {
                "total_s": 10.0,
                "functions": [
                    {"file": SRC, "line": 36, "function": "frequentWords", "ncalls": 0, "tottime": 6.0, "cumtime": 9.0},
                    {"file": SRC, "line": 19, "function": "uniqueWords", "ncalls": 0, "tottime": 3.0, "cumtime": 3.0},
                    {"file": "bench/run.js", "line": 1, "function": "", "ncalls": 0, "tottime": 1.0, "cumtime": 10.0},
                ],
            }  # fmt: skip
            return ExecResult(0, "JOULECODE_JSON" + json.dumps(stats), 0.1)
        if env and "--cpu-prof" in env.get("NODE_OPTIONS", ""):
            assert command.startswith("mkdir -p ") and "BENCH_ROUNDS" in env
            return ExecResult(0, "rounds=10", 1.0)
        src = (self.path / SRC).read_text()
        cost = 1.0 + ("seen.includes" in src) * 1.0 + ("for (const other of all)" in src) * 2.0
        self.cpu += cost
        return ExecResult(0, "ok", cost)

    def cpu_seconds(self):
        return self.cpu

    def remove(self):
        pass


class JsRunner:
    def __init__(self) -> None:
        self.images: dict[str, Path] = {}

    def build(self, context, dockerfile, tag):
        self.images[tag] = Path(context)
        return tag

    def start(self, image, limits):
        return JsBox(self.images[image])


@needs_tools
def test_reviewer_and_documenter_for_js(lang, tmp_path):
    c = Candidate("c1", SRC, "frequentWords", "t", "r", [], [{"name": "frequentWords", "code": API_BREAK}])
    materialize(c, EXAMPLE, tmp_path / "c1", lang)
    review = Reviewer(None, ReviewSettings(), lang=lang).review(c, EXAMPLE)
    assert review["veto"] and "Signature" in review["rule_vetoes"][0]

    c2 = Candidate(
        "c2", SRC, "frequentWords", "t", "r", [], [{"name": "frequentWords", "code": OPT_FREQUENT}]
    )
    materialize(c2, EXAMPLE, tmp_path / "c2", lang)
    actions = Documenter(None, lang=lang).ensure_docstrings(c2, EXAMPLE)
    assert "JSDoc comment of the original restored" in actions[0]
    src = (c2.path / SRC).read_text()
    assert "ties sorted alphabetically" in src and "@returns {Array<[string, number]>}" in src


@needs_tools
def test_js_pipeline_end_to_end_with_fakes(lang, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    llm = JsScriptedLLM()
    pipe = Pipeline(cfg, JsRunner(), CgroupCpuBackend(), llm, log=lambda _: None, wait_gpu=lambda: True)
    result = pipe.run(repo, tmp_path / "work")

    assert result.status == "ok", result.error
    assert result.language == "javascript" and result.profile["profiler"] == "V8 --cpu-prof"
    assert [h["qualname"] for h in result.profile["hotspots"]] == ["frequentWords", "uniqueWords"]
    by_title = {c.title: c for c in result.candidates}
    # object keys change the order of numeric words → test red → repair with Set
    assert by_title["object keys"].origin == "repair" and by_title["object keys"].status == "measured"
    assert "numeric words" in by_title["object keys"].messages[0] or by_title["object keys"].tests_output
    # signature change → veto → revision
    assert by_title["new parameter"].status == "vetoed"
    assert (
        by_title["counting with Map"].origin == "revision"
        and by_title["counting with Map"].status == "measured"
    )
    assert all("JavaScript" in s for s in llm.systems)
    assert any("```js" in p and "sampling profiler" in p for role, p in llm.calls if role == "optimizer")
    combo = result.combination
    assert combo and combo.status == "measured"
    combined = (combo.path / SRC).read_text()
    assert "new Set(words(text))" in combined and "counts.get(word)" in combined

    write_optimize_reports(result, tmp_path / "out")
    assert "JavaScript/TypeScript files" in (tmp_path / "out" / "analysis.md").read_text()
    assert "V8 --cpu-prof" in (tmp_path / "out" / "report.html").read_text()


@needs_tools
def test_js_canonical_ignores_line_breaks_but_not_strings(lang):
    a = "export function f(xs) {\n  return Array.from(xs).sort().slice(0, 2);\n}\n"
    b = "export function f(xs) {\n  return Array.from(xs)\n    .sort()   // sort\n    .slice(0, 2);\n}\n"
    assert lang.canonical(a) == lang.canonical(b)
    assert lang.canonical("const s = 'a b';") != lang.canonical("const s = 'a  b';")


# For the hotspot frequentWords the LLM rewrites both functions; for uniqueWords it returns
# the same code with a different line break (as in the run of 13 October on the measurement machine).
def _both(title: str, frequent: str) -> dict:
    return {"title": title, "rationale": "Set and Map.", "imports": [],
            "functions": [{"name": "uniqueWords", "code": OPT_UNIQUE}, {"name": "frequentWords", "code": frequent}]}  # fmt: skip


class BothHotspotsLLM(JsScriptedLLM):
    def generate_json(self, system, prompt, schema, *, max_tokens=None):
        if schema is prompts.OPTIMIZER_SCHEMA:
            self.calls.append(("optimizer", prompt))
            head = prompt.split("\n", 1)[0]
            if "frequentWords" in head:
                return {"candidates": [_both("Set and Map", OPT_FREQUENT)]}
            reflowed = OPT_FREQUENT.replace(
                "  const pairs = [...counts];\n", "  const pairs =\n    [...counts];\n"
            )
            return {
                "candidates": [_both("Set and Map (reflowed)", reflowed), _proposal("Set only", OPT_UNIQUE)]
            }
        return super().generate_json(system, prompt, schema, max_tokens=max_tokens)


@needs_tools
def test_candidate_for_two_hotspots_is_grouped_and_deduplicated(lang, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    pipe = Pipeline(
        cfg, JsRunner(), CgroupCpuBackend(), BothHotspotsLLM(), log=lambda _: None, wait_gpu=lambda: True
    )
    result = pipe.run(repo, tmp_path / "work")
    assert result.status == "ok", result.error
    by_title = {c.title: c for c in result.candidates}
    both = by_title["Set and Map"]
    assert both.hotspot == "frequentWords + uniqueWords" and both.status == "measured"
    assert by_title["Set and Map (reflowed)"].status == "duplicate"
    assert both.id in by_title["Set and Map (reflowed)"].messages[-1]
    assert by_title["Set only"].hotspot == "uniqueWords"
    # The candidate for both hotspots already is the combination – no second measurement
    assert result.combination is None
    assert any(w.startswith("No combination needed") for w in result.warnings)
    groups = {r["hotspot"] for r in result.ranking}
    assert groups == {"frequentWords + uniqueWords", "uniqueWords"}
