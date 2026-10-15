"""PHP back-end: code operations, analysis, Xdebug analysis, pipeline with fakes.

Most tests need a local `php` and nikic/php-parser:
JOULECODE_PHP_AUTOLOAD=<path/to/vendor/autoload.php> pytest tests/test_php.py
Without this variable they are skipped (the pure Python parts always run).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from joulecode import prompts
from joulecode.agents import Candidate, Documenter, Reviewer, ReviewSettings, materialize
from joulecode.config import load_config
from joulecode.container import ExecResult
from joulecode.energy.cgroup_cpu import CgroupCpuBackend
from joulecode.lang import get_language
from joulecode.lang import php as php_backend
from joulecode.lang.php import PhpLanguage, PhpTools, map_xdebug_name, parse_phpmd, parse_rector
from joulecode.optimize import Pipeline
from joulecode.optimize_report import write_optimize_reports

EXAMPLE = Path(__file__).parent.parent / "examples" / "php-wordstats"
SRC = "src/WordStats.php"
HAS_PHP = shutil.which("php") is not None
HAS_PARSER = HAS_PHP and bool(os.environ.get("JOULECODE_PHP_AUTOLOAD"))
needs_php = pytest.mark.skipif(not HAS_PHP, reason="needs php")
needs_parser = pytest.mark.skipif(not HAS_PARSER, reason="needs php and JOULECODE_PHP_AUTOLOAD (php-parser)")


@pytest.fixture
def lang(monkeypatch) -> PhpLanguage:
    monkeypatch.setenv("JOULECODE_PHP_TOOLS", "local")
    monkeypatch.setattr(php_backend, "_TOOLS", None)
    return PhpLanguage(PhpTools(mode="local"))


CLASS_SRC = """<?php

declare(strict_types=1);

namespace App\\Text;

use App\\Util\\Helper;

final class Parser
{
    /**
     * Old.
     */
    #[\\Deprecated]
    public static function parse(int $x): int
    {
        return $x + 1;
    }

    private function other(): Helper { return new Helper(); }
}
"""


# -- pure Python parts ------------------------------------------------------------------
def test_get_language():
    assert get_language("php").name == "php"
    assert get_language().name == "python"
    with pytest.raises(ValueError):
        get_language("cobol")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("WordStats\\WordStats::uniqueWords", "WordStats::uniqueWords"),
        ("App\\Text\\Parser->parse", "Parser::parse"),
        ("App\\helper", "helper"),
        ("php::in_array", None),
        ("{main}", None),
        ("{closure:/app/src/A.php:12-14}", None),
        ("require_once::/app/vendor/autoload.php", None),
    ],
)
def test_map_xdebug_name(name, expected):
    assert map_xdebug_name(name) == expected


def test_parse_rector_one_finding_per_hunk():
    out = json.dumps(
        {
            "file_diffs": [
                {
                    "file": "src/T.php",
                    "diff": "--- Original\n+++ New\n@@ -28,8 +28,6 @@\n         }\n     }\n-\n"
                    "-    private function other(): Helper { return new Helper(); }\n }\n",
                    "applied_rectors": [
                        "Rector\\DeadCode\\Rector\\ClassMethod\\RemoveUnusedPrivateMethodRector"
                    ],
                }
            ]
        }
    )
    (f,) = parse_rector("Warning first\n" + out, "/repo")
    assert (f.file, f.line, f.code) == ("src/T.php", 31, "RemoveUnusedPrivateMethod")
    assert "private function other" in f.message


def test_parse_phpmd_relative_paths():
    out = json.dumps(
        {
            "files": [
                {
                    "file": "/repo/src/A.php",
                    "violations": [
                        {
                            "beginLine": 7,
                            "rule": "UnusedPrivateMethod",
                            "description": "Avoid unused private methods.",
                        }
                    ],
                }
            ]
        }
    )
    (f,) = parse_phpmd(out, "/repo")
    assert (f.tool, f.file, f.line) == ("phpmd", "src/A.php", 7)


def test_prompts_render_for_php():
    text = prompts.render(prompts.OPTIMIZER_SYSTEM, get_language("php").prompts)
    assert "PHP performance engineer" in text and "{" not in text.replace("{main}", "")
    assert "```php" in prompts.render(
        prompts.REPAIR_FEEDBACK, get_language("php").prompts, title="t", output="o", code="c"
    )


def test_php_config_accepts_profile_setup():
    cfg = load_config(EXAMPLE)
    assert cfg.language == "php"
    assert "composer.json" in cfg.protected_paths


# -- Xdebug analysis (needs php only) -------------------------------------------------
CACHEGRIND = """version: 1
creator: xdebug 3.3.2 (PHP 8.3.6)
cmd: /app/bench/run.php
part: 1
positions: line

events: Time_(10ns) Memory_(bytes)

fl=(1) /app/src/WordStats.php
fn=(1) WordStats\\WordStats::uniqueWords
17 500000 100

fl=(2) php:internal
fn=(2) php::in_array
21 300000 0

fl=(1)
fn=(1)
17 400000 100
cfl=(2)
cfn=(2)
calls=1 0 0
21 300000 0

fl=(3) /app/bench/run.php
fn=(3) {main}
1 10000 0
cfl=(1)
cfn=(1)
calls=2 0 0
40 1200000 0

summary: 1210000 200
"""


@needs_php
def test_cachegrind_aggregates_self_and_inclusive_time(tmp_path):
    app = tmp_path / "app"
    (app / "prof").mkdir(parents=True)
    (app / "prof" / "cachegrind.out.1").write_text(CACHEGRIND.replace("/app/", f"{app}/"))
    res = subprocess.run(
        ["php", str(php_backend.TOOLS_DIR / "cachegrind.php"), str(app / "prof"), json.dumps(["bench"])],
        cwd=app,
        capture_output=True,
        text=True,
        check=True,
    )
    stats = json.loads(res.stdout.split("JOULECODE_JSON", 1)[1])
    assert stats["total_s"] == pytest.approx(0.0121)
    (row,) = stats["functions"]  # bench/ excluded, internal functions without a project file
    assert row["file"] == SRC and row["function"] == "WordStats\\WordStats::uniqueWords"
    assert row["ncalls"] == 2
    # self time incl. the built-in function in_array (0.003 s), like `x in some_list` in cProfile
    assert row["tottime"] == pytest.approx(0.012) and row["cumtime"] == pytest.approx(0.012)


# -- Code operations (php-parser) -----------------------------------------------------
@needs_parser
def test_functions_spans_signature_doc_complexity(lang):
    (parse, other) = lang.functions(CLASS_SRC)
    assert parse.qualname == "Parser::parse"
    assert parse.start == 11 and parse.def_line == 15 and parse.end == 18  # incl. PHPDoc and attribute
    assert parse.indent == "    " and parse.docstring == "Old."
    assert parse.signature == "parse($x:int):int[public,static]"
    src = (EXAMPLE / SRC).read_text()
    assert lang.complexity(src, "WordStats::frequentWords") == 4
    assert lang.complexity(src, "WordStats::uniqueWords") == 3


@needs_parser
def test_replace_method_keeps_class_indentation(lang):
    new = lang.replace_function(
        CLASS_SRC, "Parser::parse", "public static function parse(int $x): int\n{\n    return $x + 2;\n}"
    )
    assert "    public static function parse(int $x): int\n    {\n        return $x + 2;\n    }\n" in new
    assert "#[\\Deprecated]" not in new and "Old." not in new  # PHPDoc/attributes belong to the function
    assert "private function other()" in new


@needs_parser
def test_replace_rejects_wrong_name_extra_code_and_syntax_errors(lang):
    with pytest.raises(ValueError, match="expected"):
        lang.replace_function(CLASS_SRC, "Parser::parse", "public function other(): int { return 1; }")
    with pytest.raises(ValueError):
        lang.replace_function(CLASS_SRC, "Parser::parse", "public function parse() { return 1; }\npublic $x;")
    with pytest.raises(ValueError, match="Syntax"):
        lang.replace_function(CLASS_SRC, "Parser::parse", "public function parse() { return 1 }")


@needs_parser
def test_add_imports_after_uses_and_dedup(lang):
    imports = lang.normalize_imports(["App\\Util\\Helper", "use SplFixedArray", "  "], "")
    assert imports == ["use App\\Util\\Helper;", "use SplFixedArray;"]
    new = lang.add_imports(CLASS_SRC, imports)
    assert new.count("use App\\Util\\Helper;") == 1
    assert "use App\\Util\\Helper;\nuse SplFixedArray;\n" in new
    with pytest.raises(ValueError):
        lang.add_imports(CLASS_SRC, ["require 'x.php';"])


@needs_parser
def test_add_imports_after_namespace_without_uses(lang):
    new = lang.add_imports((EXAMPLE / SRC).read_text(), ["use SplObjectStorage;"])
    assert "namespace WordStats;\n\nuse SplObjectStorage;\n" in new


@needs_parser
def test_insert_and_replace_phpdoc(lang):
    src = "<?php\n\nfunction f(int $a): int\n{\n    return $a;\n}\n"
    with_doc = lang.insert_docstring(src, "f", "Returns a.")
    assert "/**\n * Returns a.\n */\nfunction f" in with_doc
    replaced = lang.insert_docstring(with_doc, "f", "Line 1\n\n@param int $a")
    assert "/**\n * Line 1\n *\n * @param int $a\n */\nfunction f" in replaced
    assert lang.find_function(replaced, "f").docstring == "Line 1\n\n@param int $a"


@needs_parser
def test_new_dependencies_ignore_builtins_and_internal(lang):
    after = lang.add_imports(
        CLASS_SRC, ["use SplFixedArray;", "use App\\Other\\Thing;", "use Vendor\\Pkg\\X;"]
    )
    new, third = lang.new_dependencies(CLASS_SRC, after, frozenset({"App"}))
    assert "SplFixedArray" not in new
    assert third == ["Vendor\\Pkg\\X"]


@needs_parser
def test_static_analysis_of_example(lang):
    a = lang.analyze_repo(EXAMPLE, load_config(EXAMPLE).protected_paths)
    assert a.metrics.files == 1 and a.metrics.language == "PHP"
    assert {f.qualname for f in a.functions} >= {"WordStats::uniqueWords", "WordStats::frequentWords"}
    assert 0 < a.metrics.mi_avg <= 100
    assert "WordStats" in lang.internal_modules(a)


# -- Agents and pipeline with fakes ---------------------------------------------------------
OPT_UNIQUE_WRONG = """/**
 * All words in order of their first occurrence, lower-cased.
 *
 * @return list<string>
 */
public static function uniqueWords(string $text): array
{
    return array_keys(array_flip(self::words($text)));
}"""

OPT_UNIQUE = """public static function uniqueWords(string $text): array
{
    $seen = [];
    $unique = [];
    foreach (self::words($text) as $word) {
        if (!isset($seen[$word])) {
            $seen[$word] = true;
            $unique[] = $word;
        }
    }
    return $unique;
}"""

OPT_FREQUENT = """public static function frequentWords(string $text, int $top = 10): array
{
    $counts = [];
    foreach (self::words($text) as $word) {
        $counts[$word] = ($counts[$word] ?? 0) + 1;
    }
    $pairs = [];
    foreach ($counts as $word => $n) {
        $pairs[] = [(string) $word, $n];
    }
    usort($pairs, fn (array $a, array $b): int => [$b[1], $a[0]] <=> [$a[1], $b[0]]);
    return array_slice($pairs, 0, $top);
}"""


# Changes the signature → rule veto (the behaviour would be correct).
API_BREAK = OPT_FREQUENT.replace("int $top = 10)", "int $top = 10, bool $sorted = true)")

# Runs instead of PHPUnit in the fake: the same expectations as tests/WordStatsTest.php.
MINI_TESTS = r"""<?php
require $argv[1] . '/src/WordStats.php';
use WordStats\WordStats;
$t = 'The dog and a cat and the bird and THE fish';
$checks = [
    WordStats::uniqueWords($t) === ['the', 'dog', 'and', 'a', 'cat', 'bird', 'fish'],
    WordStats::uniqueWords('') === [],
    WordStats::uniqueWords('1 01 a 1') === ['1', '01', 'a'],
    WordStats::frequentWords($t, 3) === [['and', 3], ['the', 3], ['a', 1]],
    count(WordStats::frequentWords('a b a', 10)) === 2,
    WordStats::frequentWords('1 1 a', 5) === [['1', 2], ['a', 1]],
    WordStats::report('b a b', 2) === "Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n",
];
foreach ($checks as $i => $ok) { if (!$ok) { echo "FAILED check $i\n"; exit(1); } }
echo "OK (7 tests)\n";
"""


def _proposal(title: str, code: str) -> dict:
    name = code.split("function ", 1)[1].split("(", 1)[0]
    return {"title": title, "rationale": f"{title}: hash lookup instead of linear search.", "imports": [],
            "functions": [{"name": f"WordStats::{name}", "code": code}]}  # fmt: skip


class PhpScriptedLLM:
    name = "scripted-php"

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
                    return {"candidates": [_proposal("isset (repaired)", OPT_UNIQUE)]}
                return {"candidates": [_proposal("array_flip", OPT_UNIQUE_WRONG)]}
            if "frequentWords" in head:
                if "rejected" in prompt:
                    return {"candidates": [_proposal("counting by hash", OPT_FREQUENT)]}
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


class PhpBox:
    def __init__(self, path: Path, tmp: Path) -> None:
        self.path, self.tmp = path, tmp
        self.id = "box"
        self.cpu = 0.0

    def exec(self, command, timeout_s, env=None):
        if "phpunit" in command:
            script = self.tmp / "mini_tests.php"
            script.write_text(MINI_TESTS)
            res = subprocess.run(["php", str(script), str(self.path)], capture_output=True, text=True)
            return ExecResult(res.returncode, res.stdout + res.stderr, 0.1)
        if "cachegrind.php" in command:
            assert "xdebug.mode=off" in command
            stats = {
                "total_s": 10.0,
                "functions": [
                    {"file": SRC, "line": 34, "function": "WordStats\\WordStats::frequentWords",
                     "ncalls": 40, "tottime": 5.0, "cumtime": 9.0},
                    {"file": SRC, "line": 17, "function": "WordStats\\WordStats::uniqueWords",
                     "ncalls": 120, "tottime": 4.0, "cumtime": 4.5},
                ],
            }  # fmt: skip
            return ExecResult(0, "JOULECODE_JSON" + json.dumps(stats), 0.1)
        if env and env.get("XDEBUG_MODE") == "profile":
            assert command.startswith("mkdir -p ")
            return ExecResult(0, "rounds=3", 1.0)
        src = (self.path / SRC).read_text()
        cost = 1.0 + ("in_array" in src) * 1.0 + ("foreach ($words as $other)" in src) * 2.0
        self.cpu += cost
        return ExecResult(0, "ok", cost)

    def cpu_seconds(self):
        return self.cpu

    def remove(self):
        pass


class PhpRunner:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.images: dict[str, Path] = {}
        self.dockerfiles: list[str] = []

    def build(self, context, dockerfile, tag):
        self.images[tag] = Path(context)
        self.dockerfiles.append((Path(context) / dockerfile).read_text())
        return tag

    def tag(self, image, tag):
        self.images[tag] = self.images[image]
        return tag

    def start(self, image, limits):
        return PhpBox(self.images[image], self.tmp)


@needs_parser
def test_reviewer_and_documenter_for_php(lang, tmp_path):
    c = Candidate(
        "c1",
        SRC,
        "WordStats::frequentWords",
        "t",
        "r",
        [],
        [{"name": "WordStats::frequentWords", "code": API_BREAK}],
    )
    materialize(c, EXAMPLE, tmp_path / "c1", lang)
    review = Reviewer(None, ReviewSettings(), lang=lang).review(c, EXAMPLE)
    assert review["veto"] and "Signature" in review["rule_vetoes"][0]

    c2 = Candidate(
        "c2",
        SRC,
        "WordStats::frequentWords",
        "t",
        "r",
        [],
        [{"name": "WordStats::frequentWords", "code": OPT_FREQUENT}],
    )
    materialize(c2, EXAMPLE, tmp_path / "c2", lang)
    actions = Documenter(None, lang=lang).ensure_docstrings(c2, EXAMPLE)
    assert "PHPDoc block of the original restored" in actions[0]
    src = (c2.path / SRC).read_text()
    assert "ties sorted alphabetically" in lang.find_function(src, "WordStats::frequentWords").source(
        src.splitlines(True)
    )
    assert "@return list<array{0: string, 1: int}>" in src


@needs_parser
def test_php_pipeline_end_to_end_with_fakes(lang, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    cfg = load_config(repo)
    cfg.measurement.repetitions = 4
    cfg.measurement.cooldown_s = 0
    cfg.benchmark.min_duration_s = 0
    llm = PhpScriptedLLM()
    runner = PhpRunner(tmp_path)
    pipe = Pipeline(cfg, runner, CgroupCpuBackend(), llm, log=lambda _: None, wait_gpu=lambda: True)
    result = pipe.run(repo, tmp_path / "work")

    assert result.status == "ok", result.error
    assert result.language == "php" and result.profile["profiler"] == "Xdebug"
    assert any("pecl install xdebug" in d for d in runner.dockerfiles)
    assert [h["qualname"] for h in result.profile["hotspots"]] == [
        "WordStats::frequentWords",
        "WordStats::uniqueWords",
    ]
    by_title = {c.title: c for c in result.candidates}
    # array_flip turns "1" into an int key → test red → repair → admitted
    assert by_title["array_flip"].origin == "repair" and by_title["array_flip"].status == "measured"
    # signature change → veto → revision
    assert by_title["new parameter"].status == "vetoed"
    assert (
        by_title["counting by hash"].origin == "revision"
        and by_title["counting by hash"].status == "measured"
    )
    assert all("PHP" in s for s in llm.systems)
    assert any("```php" in p for role, p in llm.calls if role == "optimizer")
    combo = result.combination
    assert combo and combo.status == "measured"
    combined = (combo.path / SRC).read_text()
    assert "isset($seen[$word])" in combined and "$counts[$word] ?? 0" in combined

    report = write_optimize_reports(result, tmp_path / "out")
    assert "PHP files" in (tmp_path / "out" / "analysis.md").read_text()
    assert "Xdebug" in (tmp_path / "out" / "report.html").read_text()
    assert report.exists()


def test_tools_image_tag_tracks_tool_files():
    tag = php_backend.tools_image()
    assert tag.startswith("joulecode/php-tools:") and len(tag.split(":")[1]) == 12
    assert textwrap.dedent(tag) == tag


@needs_parser
def test_resolve_method_name_without_class(lang):
    # During the repair, qwen2.5-coder returned "frequentWords" instead of "WordStats::frequentWords".
    src = (EXAMPLE / SRC).read_text()
    assert lang.resolve_name(src, "frequentWords") == "WordStats::frequentWords"
    assert lang.resolve_name(src, "WordStats::frequentWords") == "WordStats::frequentWords"


@needs_parser
def test_php_canonical_ignores_whitespace_and_comments(lang):
    a = "<?php\nfunction f($x) { return $x + 1; }\n"
    b = "<?php\n\nfunction f( $x )\n{\n    // add one\n    return $x+1;\n}\n"
    assert lang.canonical(a) == lang.canonical(b)
    assert lang.canonical("<?php $s = 'a b';") != lang.canonical("<?php $s = 'a  b';")
