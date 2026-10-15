"""Java back-end: JavaParser, PMD and Java Flight Recorder.

The tools run in the image `joulecode/java-tools` (Dockerfile in `java_tools/`), which joule:code
builds on first use – the measurement machine does not need Java. Locally (development, tests):
`JOULECODE_JAVA_TOOLS=local` and `JOULECODE_JAVA_CLASSPATH=<classes>:<javaparser-core.jar>`
(compiled Tool.java and JfrStats.java), PMD optionally via `JOULECODE_PMD=<path/to/bin/pmd>`.

* **Code operations:** `Tool.java` (JavaParser) returns methods and constructors of all classes
  (including nested ones) with spans incl. Javadoc and annotations, signature, complexity,
  imports and packages. Qualified names: `Class.method`, `Outer.Inner.method`,
  constructors `Class.<init>`; overloaded methods with parameter types: `Class.m(String,int)`.
* **Analysis:** complexity as in PMD, maintainability index with the same formula as for the other
  languages, PMD (category performance) as findings, PMD `Unused*` as dead code.
* **Compiling:** compilation happens in the project's Dockerfile. A compiler error of a candidate
  shows up when building the image and counts like a red test (repair with the build output).
* **New dependency:** an import that belongs neither to the JDK nor to a package of the repository and
  whose library (first two package segments) is not imported anywhere in the repository yet.
* **Profiling:** Java Flight Recorder via `JAVA_TOOL_OPTIONS` – affects every JVM of the
  benchmark (also behind Maven/Gradle), no separate image. The analysis happens in the container with
  the precompiled class `JfrStats` (requires Java ≥ 17 in the image).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

from joulecode.analysis import Finding
from joulecode.lang.base import LanguagePrompts
from joulecode.lang.tooling import ToolBox, ToolError, ToolLanguage, relative, require_json, source_files
from joulecode.lang.tooling import tools_image as _tools_image
from joulecode.profiling import Hotspot, ProfileResult, _parse_stats

TOOLS_DIR = Path(__file__).with_name("java_tools")
PROFILE_DIR = "/tmp/joulecode-prof"
# DebugNonSafepoints: without this option, JIT code only has debug info at safepoints – JFR then
# attributes samples to the next safepoint, often a loop in the caller (inlined
# methods such as `uniqueWords` disappear from the profile). With the option the attribution is correct.
JFR_OPTION = (
    "-XX:+UnlockDiagnosticVMOptions -XX:+DebugNonSafepoints "
    f"-XX:StartFlightRecording=filename={PROFILE_DIR}/rec-%p.jfr,settings=profile,dumponexit=true"
)
IMAGE_CLASSPATH = "/opt/joulecode/classes:/opt/joulecode/lib/javaparser-core.jar"
DEAD_RULES = {"UnusedPrivateMethod", "UnusedPrivateField", "UnusedLocalVariable", "UnusedFormalParameter"}
JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "org.w3c.dom", "org.xml.sax", "org.ietf.jgss")

PROMPTS = LanguagePrompts(
    language="Java",
    fence="java",
    stdlib="the JDK (java.*, javax.*) and the libraries the project already imports",
    idioms=(
        "- Readable, idiomatic Java code instead of tricks; keep Javadoc and annotations; signature "
        "incl. generics, throws and modifiers unchanged.\n"
        "- HashSet/HashMap instead of contains/indexOf on lists in loops; StringBuilder instead of string "
        "concatenation in loops; avoid unnecessary boxing (Integer in hot loops) and copies; "
        "pre-size capacity if the size is known.\n"
        "- Mind Java semantics: HashMap/HashSet have NO insertion order – if the order "
        "matters, use LinkedHashMap/LinkedHashSet; equals instead of == for objects and strings; the return value stays "
        "mutable if it was (no List.of/toList() instead of ArrayList); sorting and "
        "tie-breaking rule stay the same; toLowerCase/toUpperCase with the same locale as in the original."
    ),
    qualname_example="`Class.method` (overloaded methods: `Class.method(String,int)`)",
    import_example="`import java.util.HashMap;`",
    doc_term="Javadoc comment",
    doc_format="only the text, without /** */ and without leading asterisks",
    function_hint=(
        "Return every changed method COMPLETELY (incl. Javadoc, annotations, modifiers, generics "
        "and throws), without the surrounding class and without its indentation. As the name, use the qualified name "
        "from the task."
    ),
    lint_tool="PMD",
)


class JavaToolError(ToolError):
    pass


def tools_image() -> str:
    return _tools_image(TOOLS_DIR, "java")


class JavaTools(ToolBox):
    """Tool.java (JavaParser) and PMD – in the image joulecode/java-tools or locally."""

    tools_dir = TOOLS_DIR
    name = "java"
    interpreter = "java"
    script = "Tool"
    error_cls = JavaToolError

    def script_argv(self) -> list[str]:
        if self.mode == "local":
            cp = os.environ.get("JOULECODE_JAVA_CLASSPATH", "")
            return ["java", "-cp", cp, "Tool"]
        return ["java", "-cp", IMAGE_CLASSPATH, "Tool"]

    def local_argv(self, argv: list[str]) -> list[str]:
        if argv and argv[0] == "pmd" and os.environ.get("JOULECODE_PMD"):
            return [os.environ["JOULECODE_PMD"], *argv[1:]]
        return argv

    def jfr_stats_class(self) -> bytes:
        """Bytecode of JfrStats – copied into the benchmark container."""
        if self.mode == "local":
            for part in os.environ.get("JOULECODE_JAVA_CLASSPATH", "").split(os.pathsep):
                candidate = Path(part) / "JfrStats.class"
                if candidate.is_file():
                    return candidate.read_bytes()
            raise self.error_cls("JfrStats.class not found in JOULECODE_JAVA_CLASSPATH.")
        self.ensure_image()
        res = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                self.image,
                "cat",
                "/opt/joulecode/classes/JfrStats.class",
            ],
            capture_output=True,
        )
        if res.returncode != 0:
            raise self.error_cls(f"JfrStats.class not readable: {res.stderr.decode(errors='replace')[-500:]}")
        return res.stdout


_TOOLS: JavaTools | None = None


def default_tools() -> JavaTools:
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = JavaTools()
    return _TOOLS


def parse_pmd(output: str, root_prefix: str, _res=None) -> list[Finding]:
    """PMD JSON → findings; unused methods/fields/variables count as dead code."""
    data = json.loads(require_json(output))
    findings = []
    for f in data.get("files", []):
        file = relative(f.get("filename", ""), root_prefix)
        for v in f.get("violations", []):
            rule = v.get("rule", "")
            tool = "pmd-dead" if rule in DEAD_RULES else "pmd"
            findings.append(
                Finding(tool, rule, v.get("description", "").strip(), file, v.get("beginline", 0))
            )
    return findings


def is_jdk(package: str) -> bool:
    return any(package == p.rstrip(".") or package.startswith(p) for p in JDK_PREFIXES)


_TOP_TYPE = re.compile(
    r"^(?:(?:public|final|abstract|sealed|non-sealed|strictfp)\s+)*(?:class|interface|enum|record|@interface)\s+(\w+)",
    re.MULTILINE,
)
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def class_map(repo: Path, excluded: list[str]) -> dict[str, str]:
    """Top-level classes of the project → file (for attributing the JFR samples)."""
    out = {}
    for rel in source_files(repo, excluded, (".java",)):
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        pkg = m[1] + "." if (m := _PACKAGE.search(text)) else ""
        for name in {Path(rel).stem, *_TOP_TYPE.findall(text)}:
            out[pkg + name] = rel
    return out


def strip_params(qualname: str) -> str:
    return qualname.split("(", 1)[0]


class JavaLanguage(ToolLanguage):
    name = "java"
    display = "Java"
    prompts = PROMPTS
    profiler = "Java Flight Recorder"
    method_sep = "."
    suffixes = (".java",)
    lint_name, dead_name = "pmd", "pmd-dead"
    metrics_labels = ("Java", "PMD", "PMD Unused*")

    def default_tools(self) -> JavaTools:
        return default_tools()

    # Code operations ------------------------------------------------------------------
    def replace_function(self, source: str, qualname: str, new_code: str) -> str:
        span = self.find_function(source, qualname)
        if span is None:
            raise KeyError(f"Method {qualname!r} not found.")
        owner, _, name = strip_params(qualname).rpartition(".")
        code = textwrap.dedent(new_code).strip("\n") + "\n"
        parsed = self._call("parse_function", code=code, method=True, owner=owner.rsplit(".", 1)[-1])
        if parsed["name"] != name:
            raise ValueError(f"Method is called {parsed['name']!r}, expected {name!r}.")
        indented = textwrap.indent(code, span.indent, lambda line: line.strip() != "")
        lines = source.splitlines(keepends=True)
        result = "".join(lines[: span.start - 1]) + indented + "".join(lines[span.end :])
        self.check_syntax(result)
        return result

    def resolve_name(self, source: str, name: str) -> str:
        names = [f.qualname for f in self.functions(source)]
        if name in names:
            return name
        # given without class or without parameter list → unique method of the same name
        short = strip_params(name).rsplit(".", 1)[-1]
        matches = [q for q in names if strip_params(q).rsplit(".", 1)[-1] == short]
        if "(" in name:
            matches = [q for q in matches if q.endswith(name[name.index("(") :])] or matches
        return matches[0] if len(matches) == 1 else name

    # Imports ---------------------------------------------------------------------------
    def normalize_imports(self, imports: list[str], code: str) -> list[str]:
        out = []
        for raw in imports:
            stmt = raw.strip().rstrip(";").strip()
            if not stmt:
                continue
            if not stmt.startswith("import "):
                stmt = "import " + stmt
            out.append(stmt + ";")
        return out

    def add_imports(self, source: str, imports: list[str]) -> str:
        if not imports:
            return source
        info = self._call("file_info", source=source)
        existing = set(info["uses"])
        new = []
        for stmt in imports:
            norm = re.sub(r"\s+", " ", stmt.strip())
            if not re.fullmatch(r"import (static )?[A-Za-z_][\w.]*(\.\*)?;", norm):
                raise ValueError(f"Not a single import statement: {stmt!r}")
            if norm not in existing:
                new.append(norm + "\n")
                existing.add(norm)
        if not new:
            return source
        at = info["use_insert_after"]
        block = "".join(new)
        if not info["uses"]:
            block = ("\n" + block) if at else (block + "\n")
        return self.insert_lines(source, at, block)

    def new_dependencies(self, before: str, after: str, internal: frozenset[str]):
        def modules(src: str) -> set[str]:
            return set(self._call("file_info", source=src)["modules"])

        new = sorted(modules(after) - modules(before))
        third_party = sorted(
            m for m in new if not is_jdk(m) and not any(m == p or m.startswith(p + ".") for p in internal)
        )
        return new, third_party

    # Analysis -----------------------------------------------------------------------
    def internal_from(self, answer: dict) -> list[str]:
        # packages of the repository and libraries it already imports (first two package segments).
        names: set[str] = set()
        for data in answer["files"].values():
            info = data.get("info") or {}
            if info.get("package"):
                names.add(info["package"])
            for m in info.get("modules", []):
                if not is_jdk(m):
                    names.add(".".join(m.split(".")[:2]))
        return sorted(names)

    def linters(self, repo: Path, files: list[str]):
        pmd = [
            "pmd", "check", "--no-progress", "--no-cache", "--format", "json",
            "--rulesets", self.tools.tool_path("ruleset.xml"), "--dir", ",".join(files),
        ]  # fmt: skip
        return [("pmd", pmd, parse_pmd)]

    # Profiling ----------------------------------------------------------------------
    def profile(self, cfg, runner, image, repo, *, log=print) -> ProfileResult:
        try:
            jfr_class = base64.b64encode(self.tools.jfr_stats_class()).decode()
        except (ToolError, OSError) as exc:
            return ProfileResult(ok=False, profiler=self.profiler, error=str(exc))
        classes = class_map(repo, cfg.protected_paths)
        env = {**cfg.benchmark.env, **cfg.profile.env}
        env["JAVA_TOOL_OPTIONS"] = f"{env.get('JAVA_TOOL_OPTIONS', '')} {JFR_OPTION}".strip()
        command = cfg.profile.command or cfg.benchmark.command
        box = runner.start(image, cfg.limits)
        try:
            log("Profiling run (Java Flight Recorder) …")
            res = box.exec(f"mkdir -p {PROFILE_DIR} && {command}", cfg.profile.timeout_s, env)
            if not res.ok:
                return ProfileResult(
                    ok=False,
                    profiler=self.profiler,
                    error=f"Profiling run exit {res.exit_code}:\n{res.output[-2000:]}",
                )
            stats_cmd = (
                f"mkdir -p /tmp/joulecode-jfr && echo {jfr_class} | base64 -d > /tmp/joulecode-jfr/JfrStats.class "
                f"&& env -u JAVA_TOOL_OPTIONS java -cp /tmp/joulecode-jfr JfrStats {PROFILE_DIR}"
            )
            table = "\n".join(f"{k}\t{v}" for k, v in sorted(classes.items()))
            stats_res = box.exec(stats_cmd, 300, {"JOULECODE_CLASSES": table})
        finally:
            box.remove()
        try:
            stats = _parse_stats(stats_res.output)
        except (ValueError, json.JSONDecodeError) as exc:
            return ProfileResult(ok=False, profiler=self.profiler, error=str(exc))
        if not stats["functions"]:
            return ProfileResult(
                ok=False,
                profiler=self.profiler,
                error="The JFR recording contains no methods from the repository. Does the benchmark run with Java ≥ 17 "
                "(JAVA_TOOL_OPTIONS is picked up) and long enough for a number of samples (10 ms)?",
            )
        hotspots = self.select_hotspots(
            stats, repo, limit=cfg.optimize.hotspots, min_share=cfg.optimize.min_share
        )
        return ProfileResult(ok=True, profiler=self.profiler, total_s=stats["total_s"], hotspots=hotspots)

    def select_hotspots(self, stats: dict, repo: Path, *, limit: int, min_share: float) -> list[Hotspot]:
        total = stats["total_s"] or 1e-9
        hotspots: list[Hotspot] = []
        for row in sorted(stats["functions"], key=lambda r: r["tottime"], reverse=True):
            share = row["tottime"] / total
            if share < min_share or len(hotspots) >= limit:
                break
            path = repo / row["file"]
            if path.suffix != ".java" or not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            try:
                funcs = self.functions(source)
            except ValueError:
                continue
            # JFR: "Outer.Inner.method"; attribute lambdas/anonymous classes via the line.
            by_name = [f for f in funcs if strip_params(f.qualname) == row["function"]]
            by_line = [f for f in funcs if f.start <= row["line"] <= f.end]
            if len(by_name) == 1:
                span = by_name[0]
            else:
                span = next((f for f in by_name if f in by_line), None) or (by_line[-1] if by_line else None)
            if span is None or any(h.file == row["file"] and h.qualname == span.qualname for h in hotspots):
                continue
            hotspots.append(
                Hotspot(
                    file=row["file"], qualname=span.qualname, line=span.start, end_line=span.end,
                    ncalls=0, tottime=row["tottime"], cumtime=row["cumtime"], share=share,
                    code=span.source(source.splitlines(keepends=True)),
                )
            )  # fmt: skip
        return hotspots


__all__ = ["JavaLanguage", "JavaTools", "class_map", "parse_pmd"]
