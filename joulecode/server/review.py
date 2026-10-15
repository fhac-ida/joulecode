"""Review processor: quick findings without measurement, limited to the changed code.

Flow:

1. Language and protected paths from `joulecode.yml` (only these two keys –
   a review needs neither a benchmark nor a Dockerfile in the repository), otherwise language by
   file extensions, with the usual paths (tests, bench) left out.
2. Static analysis of the language back-end (`Language.analyze_repo`), the same as for
   `joulecode analyze`: Python Ruff/radon/vulture, PHP Rector/PHPMD, JS ESLint, Java PMD.
   The tools only read the code; PHP/JS/Java run in their tool images without network.
3. Map the diff: the client sends `git diff -U0` against the target branch. A finding
   belongs to the changed code if the function it is located in contains changed lines
   (the whole function, not just the line – whoever touches a function is responsible for it;
   like “New Code” in SonarQube). New, not yet versioned files count completely.
4. Rating: severity and category per rule (table below). `major` means: a known
   efficiency anti-pattern; whether it measurably costs energy in this code only `gate` can tell.

Without a diff (no `--base`) all findings of the repository are reported. With a diff only those in the
changed code, unless `options.scope == "all"`; the others then only appear as a number in
the summary.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from joulecode.server import RESULT_SCHEMA
from joulecode.server.processing import EXTENSIONS, JobContext, scan_repo

PHASES = ["unpack", "detect language", "static analysis", "map diff", "rating"]
DEFAULT_EXCLUDED = ["tests", "test", "bench", "benchmark", "joulecode.yml"]
MAX_FINDINGS = 500

# (tool, rule prefix) → (severity, category, explanation). The first match wins; "" always matches.
SEVERITY_RULES: list[tuple[str, str, str, str, str]] = [
    # Python – Ruff
    ("ruff", "PERF", "major", "performance", "Known performance anti-pattern (Ruff PERF)."),
    ("ruff", "C4", "minor", "performance", "Superfluous intermediate steps with lists/dicts/sets (Ruff C4)."),
    ("ruff", "FURB", "minor", "performance", "Cumbersome expression with a more efficient alternative (Ruff FURB)."),
    ("ruff", "SIM", "info", "maintainability", "Simplification (Ruff SIM) – readability, rarely energy."),
    ("ruff", "UP", "info", "maintainability", "Outdated syntax (Ruff UP)."),
    ("ruff", "B", "info", "maintainability", "Possible bug (Ruff B)."),
    ("vulture", "", "info", "dead-code", "Probably unused code (vulture)."),
    # PHP
    ("rector", "", "minor", "performance", "Rector suggests a simpler/more efficient way of writing this."),
    ("phpmd", "", "info", "dead-code", "Unused code (PHPMD)."),
    # JavaScript/TypeScript
    ("eslint", "no-await-in-loop", "major", "performance", "Sequential waiting in a loop."),
    ("eslint", "unicorn/prefer-set-has", "major", "performance", "Lookup in an array instead of a set."),
    ("eslint", "", "minor", "performance", "ESLint efficiency rule."),
    ("eslint-dead", "", "info", "dead-code", "Unused variable (ESLint)."),
    # Java – PMD performance
    ("pmd", "AvoidInstantiatingObjectsInLoops", "minor", "performance", "Objects are created in a loop."),
    ("pmd", "UseStringBufferForStringAppends", "major", "performance", "String concatenation in a loop."),
    ("pmd", "", "minor", "performance", "PMD rule of the category performance."),
    ("pmd-dead", "", "info", "dead-code", "Unused code (PMD)."),
    ("", "", "info", "other", ""),
]  # fmt: skip


def classify(tool: str, code: str) -> tuple[str, str, str]:
    for t, prefix, severity, category, why in SEVERITY_RULES:
        if (not t or t == tool) and code.startswith(prefix):
            return severity, category, why
    return "info", "other", ""


# -- Diff --------------------------------------------------------------------------------
_FILE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Unified diff → changed line ranges in the new state per file.

    Pure deletions (+n,0) count as a change of the line before, so that a function
    from which something was only removed still counts as changed.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    current = None
    for line in diff.splitlines():
        m = _FILE.match(line)
        if m:
            current = None if m[1] == "/dev/null" else m[1]
            if current:
                out.setdefault(current, [])
            continue
        m = _HUNK.match(line)
        if m and current:
            start, count = int(m[1]), int(m[2]) if m[2] is not None else 1
            out[current].append((max(start, 1), max(start, 1)) if count == 0 else (start, start + count - 1))
    return out


@dataclass
class ChangeSet:
    """What the PR changed. `None` = unknown (no diff) → everything counts as changed."""

    ranges: dict[str, list[tuple[int, int]]] | None
    whole_files: set[str]

    @classmethod
    def from_job(cls, diff: str | None, changed_files: list[str]) -> ChangeSet:
        if diff is None and not changed_files:
            return cls(None, set())
        ranges = changed_lines(diff or "")
        # In changed_files, but without a hunk: new, unversioned file (or no diff sent).
        whole = {f for f in changed_files if f not in ranges}
        return cls(ranges, whole)

    @property
    def known(self) -> bool:
        return self.ranges is not None

    def touches(self, file: str, start: int, end: int) -> bool:
        if self.ranges is None or file in self.whole_files:
            return True
        return any(a <= end and start <= b for a, b in self.ranges.get(file, []))

    def files(self) -> set[str]:
        return set() if self.ranges is None else set(self.ranges) | self.whole_files


# -- Configuration -----------------------------------------------------------------------
def review_settings(root: Path) -> tuple[str | None, list[str]]:
    """Language and protected paths from joulecode.yml – without the remaining validation."""
    for name in ("joulecode.yml", "joulecode.yaml"):
        path = root / name
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return None, DEFAULT_EXCLUDED
            if not isinstance(data, dict):
                return None, DEFAULT_EXCLUDED
            excluded = data.get("protected_paths") or DEFAULT_EXCLUDED
            return data.get("language"), [str(p) for p in excluded]
    return None, DEFAULT_EXCLUDED


# -- Processor ------------------------------------------------------------------------------
class ReviewProcessor:
    name = "review"
    description = "static analysis (Ruff/radon/vulture, Rector/PHPMD, ESLint, PMD)"

    def phases(self, mode: str) -> list[str]:
        return PHASES

    def run(self, ctx: JobContext) -> dict[str, Any]:
        return self.analyze(ctx, ctx.phase)

    def analyze(self, ctx: JobContext, phase=lambda name: None) -> dict[str, Any]:
        """The actual analysis; `phase` reports progress (gate calls it without its own phases)."""
        from joulecode.lang import LANGUAGES, get_language

        phase("unpack")
        phase("detect language")
        root = ctx.head_dir
        language, excluded = review_settings(root)
        warnings: list[str] = []
        if not language:
            language, _ = scan_repo(root)
            ctx.log("info", f"Language by file extensions: {language}")
        if language not in LANGUAGES:
            raise ValueError(f"Language {language!r} is not supported ({', '.join(LANGUAGES)})")
        lang = get_language(language)

        phase("static analysis")
        static = lang.analyze_repo(root, excluded)
        for err in static.tool_errors:
            ctx.log("warn", err)
            warnings.append(f"Tool error: {err}")
        ctx.log(
            "info",
            f"{static.metrics.files} files, {len(static.functions)} functions, {len(static.findings)} raw findings",
        )
        if static.metrics.files == 0:
            warnings.append(f"No {lang.display} files found (left out: {', '.join(excluded)}).")

        phase("map diff")
        changes = ChangeSet.from_job(ctx.diff, ctx.job.get("changed_files") or [])
        functions_by_file: dict[str, list] = {}
        for fn in static.functions:
            functions_by_file.setdefault(fn.file, []).append(fn)

        def enclosing(file: str, line: int):
            spans = [f for f in functions_by_file.get(file, []) if f.line <= line <= f.end_line]
            return min(spans, key=lambda f: f.end_line - f.line, default=None)

        changed_functions = [
            f for f in static.functions if changes.known and changes.touches(f.file, f.line, f.end_line)
        ]

        phase("rating")
        scope_all = (ctx.options.get("scope") == "all") or not changes.known
        findings: list[dict[str, Any]] = []
        outside = 0
        for i, f in enumerate(sorted(static.findings, key=lambda f: (f.file, f.line, f.code)), 1):
            fn = enclosing(f.file, f.line)
            start, end = (fn.line, fn.end_line) if fn else (f.line, f.line)
            in_diff = changes.known and changes.touches(f.file, start, end)
            if not in_diff and not scope_all:
                outside += 1
                continue
            severity, category, why = classify(f.tool, f.code)
            findings.append(
                {
                    "id": f"F{i}",
                    "rule": f.code or f.tool,
                    "tool": f.tool,
                    "severity": severity,
                    "category": category,
                    "evidence": "static",
                    "file": f.file,
                    "line": f.line,
                    "function": fn.qualname if fn else None,
                    # Rector only reports diff lines; they go into the suggestion, the message names the rule.
                    "message": f"Rector suggests a transformation ({f.code})"
                    if f.tool == "rector"
                    else f.message,
                    "why": why,
                    "suggestion": {
                        "description": f"automatic fix by {f.tool}",
                        "patch": f.suggestion,
                    }
                    if f.suggestion
                    else None,
                    "in_diff": bool(in_diff),
                }
            )
        rank = {"major": 0, "minor": 1, "info": 2}
        findings.sort(key=lambda x: (not x["in_diff"], rank[x["severity"]], x["file"], x["line"]))
        truncated = max(0, len(findings) - MAX_FINDINGS)
        if truncated:
            warnings.append(f"{truncated} further findings not transferred (limit {MAX_FINDINGS}).")
            findings = findings[:MAX_FINDINGS]

        if changes.known:
            ext = {
                e
                for e, lng in EXTENSIONS.items()
                if lng == language or (language == "typescript" and lng == "javascript")
            }
            changed_src = sorted(f for f in changes.files() if Path(f).suffix in ext)
            ctx.log(
                "info",
                f"{len(changed_src)} changed source file(s), {len(changed_functions)} changed function(s)",
            )
            if not changed_src:
                warnings.append("The diff contains no source files of this language – nothing to check.")

        m = static.metrics
        summary = {
            "files": m.files,
            "functions": m.functions,
            "findings": len(findings),
            "findings_outside_diff": outside,
            "by_evidence": dict(Counter(f["evidence"] for f in findings)),
            "by_severity": dict(Counter(f["severity"] for f in findings)),
            "by_category": dict(Counter(f["category"] for f in findings)),
            "language": language,
            "scope": "all" if scope_all else "changed",
            "changed_functions": len(changed_functions),
            "dummy": False,
        }
        return {
            "schema": RESULT_SCHEMA,
            "dummy": False,
            "mode": ctx.mode,
            "language": language,
            "processor": self.name,
            "findings": findings,
            "measurement": None,
            "candidates": [],
            "changed_functions": [
                {
                    "file": f.file,
                    "function": f.qualname,
                    "line": f.line,
                    "end_line": f.end_line,
                    "complexity": f.complexity,
                    "findings": sum(
                        1 for x in findings if x["file"] == f.file and x["function"] == f.qualname
                    ),
                }
                for f in sorted(changed_functions, key=lambda f: (f.file, f.line))
            ][:200],
            "metrics": {
                "language": m.language,
                "lint_tool": m.lint_tool,
                "files": m.files,
                "sloc": m.sloc,
                "functions": m.functions,
                "complexity_avg": m.complexity_avg,
                "complexity_max": m.complexity_max,
                "mi_avg": m.mi_avg,
            },
            "excluded": excluded,
            "warnings": warnings,
            "summary": summary,
        }


class ModeRouter:
    """One processor per mode – so the modes can be switched from dummy to real one by one."""

    def __init__(self, processors: dict[str, Any]):
        self.processors = processors
        names = sorted({p.name for p in processors.values()})
        self.name = " + ".join(names)

    def for_mode(self, mode: str):
        return self.processors[mode]

    def phases(self, mode: str) -> list[str]:
        return self.for_mode(mode).phases(mode)

    def run(self, ctx: JobContext) -> dict[str, Any]:
        return self.for_mode(ctx.mode).run(ctx)

    def describe(self) -> dict[str, str]:
        return {mode: p.name for mode, p in self.processors.items()}
