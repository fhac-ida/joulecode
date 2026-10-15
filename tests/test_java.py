"""Java back-end: code operations, analysis, JFR profiling, pipeline with fakes.

Most tests need a JDK (javac) and JavaParser plus compiled Tool.java/JfrStats.java:
JOULECODE_JAVA_CLASSPATH=<classes>:<javaparser-core.jar> pytest tests/test_java.py
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
from joulecode.container import BuildFailed, ExecResult
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.lang import get_language
from joulecode.lang import java as java_backend
from joulecode.lang.java import JavaLanguage, JavaTools, class_map, parse_pmd
from joulecode.optimize import Pipeline
from joulecode.optimize_report import write_optimize_reports

EXAMPLE = Path(__file__).parent.parent / "examples" / "java-wordstats"
SRC = "src/main/java/wordstats/WordStats.java"
HAS_JDK = shutil.which("javac") is not None
HAS_TOOLS = HAS_JDK and bool(os.environ.get("JOULECODE_JAVA_CLASSPATH"))
needs_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="needs a JDK and JOULECODE_JAVA_CLASSPATH (JavaParser)"
)
# Without the environment's proxy settings, so that the JVM does not log anything unrelated.
JAVA_ENV = {k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"}


@pytest.fixture
def lang(monkeypatch) -> JavaLanguage:
    monkeypatch.setenv("JOULECODE_JAVA_TOOLS", "local")
    monkeypatch.setattr(java_backend, "_TOOLS", None)
    return JavaLanguage(JavaTools(mode="local"))


SAMPLE = """package app;

import java.util.ArrayList;
import java.util.List;

/** Example. */
public class Sample {
    /**
     * Old.
     */
    @Deprecated
    public static int parse(int x) {
        return x + 1;
    }

    static int over(int a) { return a; }

    static int over(String s, int... rest) { return 1; }

    Sample() {
    }

    static class Inner {
        void run() {
            if (true && false) { }
        }
    }
}
"""


# -- pure Python parts ------------------------------------------------------------------
def test_get_language_java():
    lang = get_language("java")
    assert lang.name == "java" and lang.prompts.fence == "java"
    assert "LinkedHashMap" in prompts.render(prompts.OPTIMIZER_SYSTEM, lang.prompts)


def test_parse_pmd_splits_dead_code():
    out = json.dumps(
        {
            "formatVersion": 0,
            "files": [
                {
                    "filename": "/repo/src/A.java",
                    "violations": [
                        {
                            "beginline": 7,
                            "rule": "UseStringBufferForStringAppends",
                            "description": "Prefer StringBuilder",
                        },
                        {"beginline": 9, "rule": "UnusedPrivateMethod", "description": "Unused method"},
                    ],
                }
            ],
        }
    )
    hint, dead = parse_pmd("[WARN] something\n" + out, "/repo")
    assert (hint.tool, hint.code, hint.file, hint.line) == (
        "pmd",
        "UseStringBufferForStringAppends",
        "src/A.java",
        7,
    )
    assert dead.tool == "pmd-dead"


def test_class_map_of_example():
    assert class_map(EXAMPLE, ["src/test", "bench"]) == {"wordstats.WordStats": SRC}


# -- Code operations (JavaParser) -----------------------------------------------------
@needs_tools
def test_functions_spans_signatures_and_overloads(lang):
    funcs = {f.qualname: f for f in lang.functions(SAMPLE)}
    assert set(funcs) == {
        "Sample.parse",
        "Sample.over(int)",
        "Sample.over(String,int...)",
        "Sample.<init>",
        "Sample.Inner.run",
    }
    parse = funcs["Sample.parse"]
    assert (parse.start, parse.def_line, parse.end) == (8, 12, 14)  # Javadoc + annotation belong to it
    assert parse.indent == "    " and parse.docstring == "Old."
    assert parse.signature == "<>int parse(int x)throws()[public,static]"
    assert funcs["Sample.Inner.run"].complexity == 3
    src = (EXAMPLE / SRC).read_text()
    assert lang.complexity(src, "WordStats.frequentWords") == 5  # 1 + 2 loops + if + ?:
    assert lang.complexity(src, "WordStats.uniqueWords") == 3


@needs_tools
def test_replace_method_with_annotation_and_wrong_name(lang):
    new = lang.replace_function(
        SAMPLE, "Sample.parse", "@Deprecated\npublic static int parse(int x) {\n    return x + 2;\n}"
    )
    assert "    @Deprecated\n    public static int parse(int x) {\n        return x + 2;\n    }\n" in new
    assert "Old." not in new
    with pytest.raises(ValueError, match="expected"):
        lang.replace_function(SAMPLE, "Sample.parse", "public static int other(int x) { return x; }")
    with pytest.raises(ValueError, match="Syntax"):
        lang.replace_function(SAMPLE, "Sample.parse", "public static int parse(int x) { return x + }")
    new = lang.replace_function(SAMPLE, "Sample.over(int)", "static int over(int a) { return a * 2; }")
    assert "return a * 2;" in new and "static int over(String s, int... rest)" in new


@needs_tools
def test_resolve_names_imports_and_javadoc(lang):
    assert lang.resolve_name(SAMPLE, "parse") == "Sample.parse"
    assert lang.resolve_name(SAMPLE, "over(int)") == "Sample.over(int)"
    assert lang.resolve_name(SAMPLE, "over") == "over"  # ambiguous – stays
    new = lang.add_imports(
        SAMPLE, lang.normalize_imports(["java.util.HashMap", "import java.util.List;"], "")
    )
    assert (
        "import java.util.List;\nimport java.util.HashMap;\n" in new
        and new.count("import java.util.List;") == 1
    )
    with pytest.raises(ValueError):
        lang.add_imports(SAMPLE, ["System.out.println(1);"])
    with_doc = lang.insert_docstring(SAMPLE, "Sample.over(int)", "Returns a.")
    assert "    /**\n     * Returns a.\n     */\n    static int over(int a)" in with_doc
    replaced = lang.insert_docstring(SAMPLE, "Sample.parse", "New.\n\n@param x number")
    assert lang.find_function(replaced, "Sample.parse").docstring == "New.\n\n@param x number"
    assert "    /**\n     * New.\n     *\n     * @param x number\n     */\n    @Deprecated" in replaced


@needs_tools
def test_new_dependencies_and_canonical(lang):
    after = lang.add_imports(
        SAMPLE,
        [
            "import java.util.HashMap;",
            "import com.google.common.base.Joiner;",
            "import org.apache.commons.lang3.StringUtils;",
        ],
    )
    new, third = lang.new_dependencies(SAMPLE, after, frozenset({"app", "com.google"}))
    assert new == ["com.google.common.base", "org.apache.commons.lang3"]
    assert third == ["org.apache.commons.lang3"]
    assert lang.canonical(SAMPLE) == lang.canonical(SAMPLE.replace("return x + 1;", "return x+1; // one"))
    assert lang.canonical('class A { String s = "a b"; }') != lang.canonical('class A { String s = "a  b"; }')


@needs_tools
def test_static_analysis_of_example(lang):
    a = lang.analyze_repo(EXAMPLE, load_config(EXAMPLE).protected_paths)
    assert a.metrics.files == 1 and a.metrics.language == "Java"
    assert {f.qualname for f in a.functions} == {
        "WordStats.<init>", "WordStats.uniqueWords", "WordStats.frequentWords", "WordStats.report", "WordStats.words",
    }  # fmt: skip
    assert "wordstats" in lang.internal_modules(a)


# -- Profiling with real JFR -------------------------------------------------------------
def _compile(repo: Path) -> subprocess.CompletedProcess:
    sources = [str(p) for p in (repo / "src/main/java").rglob("*.java")]
    res = subprocess.run(
        ["javac", "-d", str(repo / "out/main"), *sources], capture_output=True, text=True, env=JAVA_ENV
    )
    if res.returncode == 0:
        res = subprocess.run(
            ["javac", "-cp", str(repo / "out/main"), "-d", str(repo / "out/bench"), str(repo / "bench/Run.java")],
            capture_output=True, text=True, env=JAVA_ENV,
        )  # fmt: skip
    return res


class LocalBox:
    """Really runs commands – in the repository directory instead of a container."""

    def __init__(self, path: Path) -> None:
        self.path, self.id, self.cpu = path, "local", 0.0

    def exec(self, command, timeout_s, env=None):
        res = subprocess.run(["sh", "-c", command], cwd=self.path, capture_output=True, text=True,
                             env={**JAVA_ENV, **(env or {})}, timeout=timeout_s)  # fmt: skip
        return ExecResult(res.returncode, res.stdout + res.stderr, 0.1)

    def remove(self):
        pass


class LocalRunner:
    def __init__(self, path: Path) -> None:
        self.path = path

    def start(self, image, limits):
        return LocalBox(self.path)


@needs_tools
def test_profile_with_real_flight_recorder(lang, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    assert _compile(repo).returncode == 0
    cfg = load_config(repo)
    cfg.profile.env = {"BENCH_ROUNDS": "15"}
    cfg.optimize.min_share = 0.05
    cfg.optimize.hotspots = 3
    result = lang.profile(cfg, LocalRunner(repo), "image", repo, log=lambda _: None)
    assert result.ok, result.error
    assert result.profiler == "Java Flight Recorder"
    names = [h.qualname for h in result.hotspots]
    assert names and names[0] == "WordStats.frequentWords"
    # With DebugNonSafepoints the inlined list search stays with uniqueWords (run with 1 CPU)
    assert "WordStats.uniqueWords" in names
    assert all(h.file == SRC for h in result.hotspots)


# -- Agents and pipeline with fakes ---------------------------------------------------------
# Forgets the import → compiler error when building → repair with the build output.
OPT_UNIQUE_NO_IMPORT = """public static List<String> uniqueWords(String text) {
    return new ArrayList<>(new LinkedHashSet<>(words(text)));
}"""

# HashSet loses the order → test “OrderIsNotHashOrder” fails.
OPT_UNIQUE_HASHSET = """public static List<String> uniqueWords(String text) {
    return new ArrayList<>(new HashSet<>(words(text)));
}"""

OPT_FREQUENT = """public static List<Map.Entry<String, Integer>> frequentWords(String text, int top) {
    Map<String, Integer> counts = new LinkedHashMap<>();
    for (String word : words(text)) {
        counts.merge(word, 1, Integer::sum);
    }
    List<Map.Entry<String, Integer>> entries = new ArrayList<>(counts.entrySet());
    entries.sort((a, b) -> a.getValue().equals(b.getValue())
            ? a.getKey().compareTo(b.getKey())
            : b.getValue() - a.getValue());
    return new ArrayList<>(entries.subList(0, Math.min(top, entries.size())));
}"""

# Return type ArrayList instead of List: compiles for all callers but changes the (binary) API → rule veto.
API_BREAK = OPT_FREQUENT.replace(
    "public static List<Map.Entry<String, Integer>>", "public static ArrayList<Map.Entry<String, Integer>>", 1
)

MINI_TEST = r"""
import java.util.*;
import wordstats.WordStats;

public class MiniTest {
    static void check(boolean ok, String name) { if (!ok) { System.out.println("FAILED " + name); System.exit(1); } }
    public static void main(String[] a) {
        String t = "The dog and a cat and the bird and THE fish";
        check(WordStats.uniqueWords(t).equals(List.of("the", "dog", "and", "a", "cat", "bird", "fish")), "order");
        check(WordStats.uniqueWords("zebra apple moon zebra tree 10 9 apple").equals(List.of("zebra", "apple", "moon", "tree", "10", "9")), "hashorder");
        check(WordStats.uniqueWords("").isEmpty(), "empty");
        check(WordStats.frequentWords(t, 3).equals(List.of(Map.entry("and", 3), Map.entry("the", 3), Map.entry("a", 1))), "ties");
        check(WordStats.frequentWords("a b a", 10).size() == 2, "top");
        WordStats.frequentWords("a b a", 10).add(Map.entry("x", 1));
        check(WordStats.report("b a b", 2).equals("Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n"), "report");
        System.out.println("OK");
    }
}
"""


def _proposal(title: str, code: str, imports=()) -> dict:
    name = code.split("(", 1)[0].rsplit(" ", 1)[-1]
    return {"title": title, "rationale": f"{title}: hash instead of linear search.", "imports": list(imports),
            "functions": [{"name": name, "code": code}]}  # fmt: skip


class JavaScriptedLLM:
    name = "scripted-java"

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
                if "HashSet" in prompt and "FAILED hashorder" in prompt:
                    return {
                        "candidates": [
                            _proposal(
                                "LinkedHashSet (repaired)", OPT_UNIQUE_NO_IMPORT, ["java.util.LinkedHashSet"]
                            )
                        ]
                    }
                if (
                    "cannot find symbol" in prompt or "Symbol nicht finden" in prompt
                ):  # English or German javac
                    return {
                        "candidates": [
                            _proposal(
                                "LinkedHashSet + Import", OPT_UNIQUE_NO_IMPORT, ["java.util.LinkedHashSet"]
                            )
                        ]
                    }
                return {"candidates": [_proposal("LinkedHashSet", OPT_UNIQUE_NO_IMPORT), _proposal("HashSet", OPT_UNIQUE_HASHSET, ["java.util.HashSet"])]}  # fmt: skip
            if "frequentWords" in head:
                if "rejected" in prompt:
                    return {
                        "candidates": [
                            _proposal(
                                "counting with LinkedHashMap", OPT_FREQUENT, ["java.util.LinkedHashMap"]
                            )
                        ]
                    }
                return {"candidates": [_proposal("new parameter", API_BREAK, ["java.util.LinkedHashMap"])]}
            return {"candidates": []}
        if schema is prompts.REVIEWER_SCHEMA:
            self.calls.append(("reviewer", prompt))
            return {"maintainability": 4, "concerns": [], "suggestion": "", "veto": False}
        if schema is prompts.DOC_SCHEMA:
            return {"docstring": "Most frequent words."}
        raise AssertionError("unknown schema")

    def unload(self) -> None:
        self.unloaded = True


class JavaBox:
    def __init__(self, path: Path, tmp: Path) -> None:
        self.path, self.tmp, self.id, self.cpu = path, tmp, "box", 0.0

    def exec(self, command, timeout_s, env=None):
        if "junit" in command:
            (self.tmp / "MiniTest.java").write_text(MINI_TEST)
            res = subprocess.run(
                ["java", "-cp", str(self.path / "out/main"), str(self.tmp / "MiniTest.java")],
                capture_output=True, text=True, env=JAVA_ENV,
            )  # fmt: skip
            return ExecResult(res.returncode, res.stdout + res.stderr, 0.1)
        if "JfrStats" in command:
            assert "wordstats.WordStats\t" + SRC in env["JOULECODE_CLASSES"]
            stats = {
                "total_s": 10.0,
                "functions": [
                    {"file": SRC, "function": "WordStats.frequentWords", "line": 45, "ncalls": 0, "tottime": 6.0, "cumtime": 9.0},
                    {"file": SRC, "function": "WordStats.uniqueWords", "line": 26, "ncalls": 0, "tottime": 3.0, "cumtime": 3.0},
                ],
            }  # fmt: skip
            return ExecResult(0, "JOULECODE_JSON" + json.dumps(stats), 0.1)
        if env and "StartFlightRecording" in env.get("JAVA_TOOL_OPTIONS", ""):
            return ExecResult(0, "rounds=30", 1.0)
        src = (self.path / SRC).read_text()
        cost = 1.0 + ("seen.contains" in src) * 1.0 + ("for (String other : all)" in src) * 2.0
        self.cpu += cost
        return ExecResult(0, "ok", cost)

    def cpu_seconds(self):
        return self.cpu

    def remove(self):
        pass


class JavaRunner:
    """Compiles like the Dockerfile does during the build – a compiler error becomes BuildFailed."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.images: dict[str, Path] = {}
        self.build_failures = 0

    def build(self, context, dockerfile, tag):
        res = _compile(Path(context))
        if res.returncode != 0:
            self.build_failures += 1
            raise BuildFailed(f"Image {tag} could not be built", res.stdout + res.stderr)
        self.images[tag] = Path(context)
        return tag

    def start(self, image, limits):
        return JavaBox(self.images[image], self.tmp)


@needs_tools
def test_reviewer_and_documenter_for_java(lang, tmp_path):
    c = Candidate(
        "c1",
        SRC,
        "WordStats.frequentWords",
        "t",
        "r",
        [],
        [{"name": "WordStats.frequentWords", "code": API_BREAK}],
    )
    materialize(c, EXAMPLE, tmp_path / "c1", lang)
    review = Reviewer(None, ReviewSettings(), lang=lang).review(c, EXAMPLE)
    assert review["veto"] and "Signature" in review["rule_vetoes"][0]

    c2 = Candidate("c2", SRC, "WordStats.frequentWords", "t", "r", ["java.util.LinkedHashMap"],
                   [{"name": "WordStats.frequentWords", "code": OPT_FREQUENT}])  # fmt: skip
    materialize(c2, EXAMPLE, tmp_path / "c2", lang)
    actions = Documenter(None, lang=lang).ensure_docstrings(c2, EXAMPLE)
    assert "Javadoc comment of the original restored" in actions[0]
    src = (c2.path / SRC).read_text()
    assert "@return pairs (word, count)" in src and "import java.util.LinkedHashMap;" in src


@needs_tools
def test_java_pipeline_end_to_end_with_fakes(lang, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    llm = JavaScriptedLLM()
    runner = JavaRunner(tmp_path)
    pipe = Pipeline(cfg, runner, CgroupCpuBackend(), llm, log=lambda _: None, wait_gpu=lambda: True)
    result = pipe.run(repo, tmp_path / "work")

    assert result.status == "ok", result.error
    assert result.language == "java" and result.profile["profiler"] == "Java Flight Recorder"
    assert [h["qualname"] for h in result.profile["hotspots"]] == [
        "WordStats.frequentWords",
        "WordStats.uniqueWords",
    ]
    by_title = {c.title: c for c in result.candidates}
    # missing import → compiler error when building → repair with the build output → admitted
    assert runner.build_failures >= 1
    no_import = by_title["LinkedHashSet"]
    assert no_import.origin == "repair" and no_import.status == "measured"
    assert "import java.util.LinkedHashSet;" in (no_import.path / SRC).read_text()
    # HashSet: compiles but loses the order → test red → repair → duplicate of the first one
    assert by_title["HashSet"].status in {"duplicate", "measured"}
    # signature change → veto → revision
    assert by_title["new parameter"].status == "vetoed"
    assert by_title["counting with LinkedHashMap"].status == "measured"
    assert all("Java-Performance" in s for s in llm.systems if "Performance" in s)
    combo = result.combination
    assert combo and combo.status == "measured"
    combined = (combo.path / SRC).read_text()
    assert (
        "new LinkedHashSet<>(words(text))" in combined and "counts.merge(word, 1, Integer::sum)" in combined
    )

    write_optimize_reports(result, tmp_path / "out")
    assert "Java files" in (tmp_path / "out" / "analysis.md").read_text()
    assert "Java Flight Recorder" in (tmp_path / "out" / "report.html").read_text()
