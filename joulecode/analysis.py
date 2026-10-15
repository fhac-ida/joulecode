"""Static analysis with existing tools and maintainability metrics for the whole repository.

* Ruff (PERF, C4, SIM, FURB, UP, B): known inefficiency and style patterns –
  serve as findings for the optimiser, not as a measure of success.
* radon: cyclomatic complexity per function, maintainability index per
  file, lines of code.
* vulture: dead code.
* Import coupling: how many internal modules a module imports (fan-out)
  or by how many it is imported (fan-in).

All .py files are analysed except the protected paths (tests,
benchmark) and auxiliary directories.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUFF_SELECT = "PERF,C4,SIM,FURB,UP,B"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "build", "dist", ".mypy_cache"}


@dataclass
class Finding:
    tool: str
    code: str
    message: str
    file: str
    line: int
    # Proposed change as diff lines ("-old", "+new") if the tool provides one
    # (Ruff fixes, Rector). The platform shows it as a suggestion in the review.
    suggestion: str | None = None


@dataclass
class FunctionMetrics:
    file: str
    qualname: str
    line: int
    end_line: int
    complexity: int
    rank: str


@dataclass
class FileMetrics:
    file: str
    sloc: int
    mi: float


@dataclass
class RepoMetrics:
    language: str = "Python"
    lint_tool: str = "Ruff"
    dead_code_tool: str = "vulture"
    files: int = 0
    sloc: int = 0
    functions: int = 0
    complexity_avg: float = 0.0
    complexity_max: int = 0
    complex_functions: int = 0  # radon rank C or worse (CC > 10)
    mi_avg: float = 0.0  # weighted by SLOC
    mi_min: float = 0.0
    lint_findings: int = 0  # findings of the linter (Python: Ruff, PHP: Rector)
    dead_code: int = 0
    fan_out_max: int = 0
    fan_out_avg: float = 0.0


@dataclass
class StaticAnalysis:
    metrics: RepoMetrics
    functions: list[FunctionMetrics] = field(default_factory=list)
    files: list[FileMetrics] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    coupling: dict[str, dict[str, int]] = field(default_factory=dict)
    tool_errors: list[str] = field(default_factory=list)
    # Names defined in the repository (PHP: classes/functions and namespace roots) – not an external dependency.
    internal_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def findings_in(self, file: str, start: int, end: int) -> list[Finding]:
        return [f for f in self.findings if f.file == file and start <= f.line <= end]

    def function(self, file: str, qualname: str) -> FunctionMetrics | None:
        return next((f for f in self.functions if f.file == file and f.qualname == qualname), None)


def python_files(repo: Path, excluded: list[str]) -> list[str]:
    out = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if any(part in SKIP_DIRS or part.startswith(".") for part in Path(rel).parts[:-1]):
            continue
        if any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excluded):
            continue
        out.append(rel)
    return out


# -- Ruff -------------------------------------------------------------------
def run_ruff(repo: Path, files: list[str]) -> list[Finding]:
    if not files:
        return []
    cmd = [
        sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--exit-zero",
        "--select", RUFF_SELECT, "--output-format", "json", *files,
    ]  # fmt: skip
    res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=True)
    findings = []
    sources: dict[str, list[str]] = {}
    for item in json.loads(res.stdout or "[]"):
        file = Path(item["filename"]).resolve().relative_to(repo.resolve()).as_posix()
        suggestion = None
        if item.get("fix") and item["fix"].get("edits"):
            if file not in sources:
                sources[file] = (repo / file).read_text(encoding="utf-8").splitlines()
            suggestion = fix_patch(sources[file], item["fix"]["edits"])
        findings.append(
            Finding("ruff", item["code"] or "", item["message"], file, item["location"]["row"], suggestion)
        )
    return findings


def fix_patch(lines: list[str], edits: list[dict]) -> str | None:
    """Ruff edits (1-based lines/columns) → diff lines of the affected lines."""
    first = min(e["location"]["row"] for e in edits)
    last = max(e["end_location"]["row"] for e in edits)
    if first < 1 or last > len(lines) or last - first > 40:
        return None
    old = lines[first - 1 : last]
    text = "\n".join(old)

    def offset(loc: dict) -> int:
        row, col = loc["row"] - first, loc["column"] - 1
        return sum(len(line) + 1 for line in old[:row]) + col

    for e in sorted(edits, key=lambda e: (e["location"]["row"], e["location"]["column"]), reverse=True):
        text = text[: offset(e["location"])] + e["content"] + text[offset(e["end_location"]) :]
    new = text.split("\n")
    if new == old:
        return None
    return "\n".join([*(f"-{line}" for line in old), *(f"+{line}" for line in new)])


# -- vulture ----------------------------------------------------------------
_VULTURE_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<msg>.+?) \((?P<conf>\d+)% confidence")


def run_vulture(repo: Path, files: list[str], min_confidence: int = 80) -> list[Finding]:
    if not files:
        return []
    cmd = [sys.executable, "-m", "vulture", f"--min-confidence={min_confidence}", *files]
    res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    findings = []
    for line in res.stdout.splitlines():
        m = _VULTURE_LINE.match(line)
        if m:
            findings.append(
                Finding("vulture", "dead-code", m["msg"], Path(m["file"]).as_posix(), int(m["line"]))
            )
    return findings


# -- radon ------------------------------------------------------------------
def function_complexities(source: str, file: str) -> list[FunctionMetrics]:
    from radon.complexity import cc_rank, cc_visit

    out = []
    for block in cc_visit(source):
        if block.__class__.__name__ == "Class":
            out.extend(
                FunctionMetrics(
                    file, f"{block.name}.{method.name}", method.lineno, method.endline,
                    method.complexity, cc_rank(method.complexity),
                )
                for method in block.methods
            )  # fmt: skip
        else:
            out.append(
                FunctionMetrics(
                    file, block.name, block.lineno, block.endline, block.complexity, cc_rank(block.complexity)
                )
            )
    return out


def file_metrics(source: str, file: str) -> FileMetrics:
    from radon.metrics import mi_visit
    from radon.raw import analyze

    return FileMetrics(file, analyze(source).sloc, round(mi_visit(source, multi=True), 2))


def maintainability_index(source: str) -> float:
    from radon.metrics import mi_visit

    return round(mi_visit(source, multi=True), 2)


def complexity_of(code: str) -> int:
    """Cyclomatic complexity of a single function (source code)."""
    import textwrap

    from radon.complexity import cc_visit

    blocks = cc_visit(textwrap.dedent(code))
    return max((b.complexity for b in blocks), default=1)


# -- Coupling ---------------------------------------------------------------
def _module_name(file: str) -> str:
    parts = list(Path(file).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def coupling(repo: Path, files: list[str]) -> dict[str, dict[str, int]]:
    modules = {_module_name(f): f for f in files}
    tops = {m.split(".")[0] for m in modules}
    imports: dict[str, set[str]] = {}
    for mod, file in modules.items():
        targets: set[str] = set()
        tree = ast.parse((repo / file).read_text(encoding="utf-8"))
        package = mod.rsplit(".", 1)[0] if "." in mod else ""
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")[: len(package.split(".")) - node.level + 1] if package else []
                    names = [".".join([*base, node.module] if node.module else base)]
                elif node.module:
                    names = [node.module]
            for name in names:
                if name.split(".")[0] in tops:
                    match = max(
                        (m for m in modules if name == m or name.startswith(m + ".")), key=len, default=None
                    )
                    if match and match != mod:
                        targets.add(match)
        imports[mod] = targets
    fan_in = Counter(t for ts in imports.values() for t in ts)
    return {m: {"fan_out": len(imports[m]), "fan_in": fan_in.get(m, 0)} for m in modules}


# -- Overall ----------------------------------------------------------------
def analyze_repo(repo: Path, excluded: list[str]) -> StaticAnalysis:
    files = python_files(repo, excluded)
    result = StaticAnalysis(metrics=RepoMetrics())
    for file in files:
        source = (repo / file).read_text(encoding="utf-8")
        try:
            result.functions.extend(function_complexities(source, file))
            result.files.append(file_metrics(source, file))
        except SyntaxError as exc:
            result.tool_errors.append(f"radon: {file}: {exc}")
    for name, runner in (("ruff", run_ruff), ("vulture", run_vulture)):
        try:
            result.findings.extend(runner(repo, files))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            result.tool_errors.append(f"{name}: {exc}")
    try:
        result.coupling = coupling(repo, files)
    except SyntaxError as exc:
        result.tool_errors.append(f"coupling: {exc}")

    fill_metrics(result, lint_tool="ruff", dead_tool="vulture")
    result.metrics.files = len(files)
    return result


def fill_metrics(result: StaticAnalysis, *, lint_tool: str, dead_tool: str) -> None:
    """Repository metrics from the individual values (the same for all languages)."""
    m = result.metrics
    m.files = len(result.files)
    m.sloc = sum(f.sloc for f in result.files)
    m.functions = len(result.functions)
    if result.functions:
        m.complexity_avg = round(sum(f.complexity for f in result.functions) / m.functions, 2)
        m.complexity_max = max(f.complexity for f in result.functions)
        m.complex_functions = sum(1 for f in result.functions if f.complexity > 10)
    if result.files:
        weight = sum(max(f.sloc, 1) for f in result.files)
        m.mi_avg = round(sum(f.mi * max(f.sloc, 1) for f in result.files) / weight, 2)
        m.mi_min = min(f.mi for f in result.files)
    m.lint_findings = sum(1 for f in result.findings if f.tool == lint_tool)
    m.dead_code = sum(1 for f in result.findings if f.tool == dead_tool)
    if result.coupling:
        outs = [c["fan_out"] for c in result.coupling.values()]
        m.fan_out_max = max(outs)
        m.fan_out_avg = round(sum(outs) / len(outs), 2)


def render_analysis_markdown(static: StaticAnalysis, profile: dict | None) -> str:
    m = static.metrics
    lines = ["# joule:code analysis", "", "## Maintainability of the repository", ""]
    lines += [
        "| Metric | Value |",
        "|---|---|",
        f"| {m.language} files / lines of code | {m.files} / {m.sloc} |",
        f"| Functions | {m.functions} |",
        f"| Complexity avg / max | {m.complexity_avg} / {m.complexity_max} |",
        f"| Functions with complexity > 10 | {m.complex_functions} |",
        f"| Maintainability index avg / min | {m.mi_avg} / {m.mi_min} |",
        f"| {m.lint_tool} findings | {m.lint_findings} |",
        f"| Dead code ({m.dead_code_tool}) | {m.dead_code} |",
        f"| Import coupling fan-out avg / max | {m.fan_out_avg} / {m.fan_out_max} |",
        "",
    ]
    if profile:
        lines += ["## Hotspots (profiling of the benchmark)", ""]
        if not profile.get("ok"):
            lines += [f"Profiling failed: {profile.get('error')}", ""]
        else:
            lines += [
                f"| Function | File | Share of self time | Calls | Complexity | {m.lint_tool} findings |",
                "|---|---|---|---|---|---|",
            ]
            for h in profile["hotspots"]:
                fm = static.function(h["file"], h["qualname"])
                hints = ", ".join(
                    sorted({f.code for f in static.findings_in(h["file"], h["line"], h["end_line"])})
                )
                lines.append(
                    f"| `{h['qualname']}` | {h['file']}:{h['line']} | {h['share']:.0%} | {h['ncalls'] or '–'} "
                    f"| {fm.complexity if fm else '–'} | {hints or '–'} |"
                )
            lines.append("")
    if static.findings:
        lines += [
            "## Findings of the tools",
            "",
            "| Tool | Rule | Location | Message |",
            "|---|---|---|---|",
        ]
        lines += [f"| {f.tool} | {f.code} | {f.file}:{f.line} | {f.message} |" for f in static.findings]
        lines.append("")
    if static.tool_errors:
        lines += ["## Tool errors", ""] + [f"- {e}" for e in static.tool_errors] + [""]
    return "\n".join(lines)
