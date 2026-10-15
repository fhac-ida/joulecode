"""Processing of a job.

The worker calls `Processor.run(ctx)` for every job and stores the
returned result (schema `joulecode-platform-result/1`, see below).
`DummyProcessor` runs through the phases of the respective
mode, takes a superficial look at the uploaded repository (language, functions)
and generates plausible but **made-up** findings and measurements from it. Every
result carries `"dummy": true`, and the web interface shows this clearly.

The real processing is implemented as further `Processor` classes:

    review    joulecode.analysis (without profiling) on the changed functions
    gate      joulecode.protocol: measure target branch vs. PR head
    optimize  joulecode.optimize

Result schema (all modes; unused parts are null or empty):

    schema        "joulecode-platform-result/1"
    dummy         bool
    mode          review | gate | optimize
    language      python | php | javascript | typescript | java | unknown
    summary       {files, functions, findings, by_evidence: {static, llm, measured}, by_severity}
    findings      [{id, rule, tool, severity: info|minor|major, evidence: static|llm|measured,
                    file, line, function, message, suggestion: {description, patch} | null}]
    measurement   null | {backend, quantity, unit, baseline: {median, n}, candidate: {median, n},
                          change, ci95: [lo, hi], p_value, verdict}
    candidates    [{id, hotspot, change, ci95, verdict, maintainability, summary}]   (optimize)
    warnings      [str]
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from joulecode.server import RESULT_SCHEMA


class Cancelled(Exception):
    pass


@dataclass
class JobContext:
    """What a processor knows about its job and how it reports progress."""

    job: dict[str, Any]
    workdir: Path
    head_dir: Path
    base_dir: Path | None
    report_phase: Callable[[str, float], None]
    log: Callable[[str, str], None]
    is_cancelled: Callable[[], bool]
    phases: list[str] = field(default_factory=list)
    # `git diff -U0` against the target branch, if the client sent one
    diff: str | None = None
    # kept after the job (measurement reports, patches); available via /api/jobs/{id}/files/…
    report_dir: Path | None = None

    @property
    def mode(self) -> str:
        return self.job["mode"]

    @property
    def options(self) -> dict[str, Any]:
        return self.job.get("options") or {}

    def phase(self, name: str) -> None:
        """Start a new phase; aborts if the job is to be cancelled."""
        if self.is_cancelled():
            raise Cancelled
        idx = self.phases.index(name) if name in self.phases else 0
        self.report_phase(name, idx / max(1, len(self.phases)))
        self.log("info", f"Phase: {name}")


class Processor(Protocol):
    name: str

    def phases(self, mode: str) -> list[str]: ...

    def run(self, ctx: JobContext) -> dict[str, Any]: ...


# -- Dummy -------------------------------------------------------------------------------
PHASES = {
    "review": ["unpack", "detect language", "static analysis", "suggestions"],
    "gate": ["unpack", "build images", "tests", "measurement", "analysis"],
    "optimize": ["unpack", "analysis + profiling", "candidates", "tests + review", "measurement", "report"],
}

EXTENSIONS = {
    ".py": "python",
    ".php": "php",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
}

FUNCTION_PATTERNS = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    "php": re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+(\w+)\s*\("),
    "javascript": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(|^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\("
    ),
    "java": re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract)\s+)+[\w<>\[\], ?]+\s+(\w+)\s*\("
    ),
}
FUNCTION_PATTERNS["typescript"] = FUNCTION_PATTERNS["javascript"]

# Typical findings of the tools that the real analysis uses.
RULES: dict[str, list[tuple[str, str, str, str]]] = {
    "python": [
        ("PERF401", "ruff", "minor", "List built with append in a loop – a list comprehension is faster"),
        ("PERF203", "ruff", "minor", "try/except inside a loop – costs on every iteration"),
        ("C416", "ruff", "info", "Unnecessary comprehension – use list()/dict() directly"),
        ("FURB118", "ruff", "info", "Lambda instead of an operator function"),
        ("membership-list", "llm", "major", "`x in some_list` in a loop – a set makes the lookup O(1)"),
        ("string-concat-loop", "llm", "major", "String is extended with += in a loop – ''.join() is linear"),
    ],
    "php": [
        ("CountInLoopCondition", "rector", "minor", "count() in the loop condition is recomputed on every iteration"),
        ("InArrayInLoop", "llm", "major", "in_array() in a loop – array_flip + isset is O(1)"),
        ("UnusedPrivateMethod", "phpmd", "info", "Private method is not used"),
        ("ArrayMergeInLoop", "llm", "major", "array_merge() in a loop copies the array every time"),
    ],
    "javascript": [
        ("unicorn/prefer-set-has", "eslint", "minor", "Array.includes() in a loop – a Set is faster"),
        ("no-await-in-loop", "eslint", "major", "await in a loop – bundle independent calls with Promise.all"),
        ("unicorn/prefer-includes", "eslint", "info", "indexOf() !== -1 instead of includes()"),
        ("spread-in-reduce", "llm", "major", "Object spread in reduce() copies the whole object at every step"),
    ],
    "java": [
        ("AvoidInstantiatingObjectsInLoops", "pmd", "minor", "Objects are newly created in a loop"),
        ("UseStringBufferForStringAppends", "pmd", "major", "String concatenation with += in a loop – use StringBuilder"),
        ("AddEmptyString", "pmd", "info", "Empty string for type conversion – use String.valueOf()"),
        ("list-contains-loop", "llm", "major", "List.contains() in a loop – a HashSet is O(1)"),
    ],
}  # fmt: skip
RULES["typescript"] = RULES["javascript"]

SUGGESTIONS = {
    "python": ("Replace the loop with a comprehension", "-    result = []\n-    for x in items:\n-        result.append(f(x))\n+    result = [f(x) for x in items]"),
    "php": ("Build the lookup table once before the loop", "-    if (in_array($w, $stop)) {\n+    $stopSet = array_flip($stop);\n+    if (isset($stopSet[$w])) {"),
    "javascript": ("Build the Set once before the loop", "-  if (stop.includes(w)) {\n+  const stopSet = new Set(stop);\n+  if (stopSet.has(w)) {"),
    "java": ("Use StringBuilder", "-        out += part;\n+        sb.append(part);"),
}  # fmt: skip
SUGGESTIONS["typescript"] = SUGGESTIONS["javascript"]


class DummyProcessor:
    """Made-up but self-consistent results – for the web interface, CI integration and tests."""

    name = "dummy"

    def __init__(self, delay_s: float = 1.0):
        self.delay_s = delay_s

    def phases(self, mode: str) -> list[str]:
        return PHASES[mode]

    def _wait(self, factor: float = 1.0) -> None:
        if self.delay_s > 0:
            time.sleep(self.delay_s * factor)

    def run(self, ctx: JobContext) -> dict[str, Any]:
        rng = random.Random(_seed(ctx.job))
        mode = ctx.mode
        phases = self.phases(mode)

        ctx.phase(phases[0])  # unpack – already done by the worker
        language, files = scan_repo(ctx.head_dir)
        ctx.log("info", f"{len(files)} source files, language: {language}")

        warnings = []
        if not (ctx.head_dir / "joulecode.yml").is_file():
            warnings.append(
                "No joulecode.yml in the repository – measurement and optimisation would need it."
            )
        if mode in ("gate", "optimize") and not (ctx.head_dir / "Dockerfile").is_file():
            warnings.append("No Dockerfile in the repository – nothing could be measured like this.")

        result: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "dummy": True,
            "mode": mode,
            "language": language,
            "processor": self.name,
            "findings": [],
            "measurement": None,
            "candidates": [],
            "warnings": warnings,
        }

        for phase in phases[1:]:
            ctx.phase(phase)
            self._wait()

        changed = set(ctx.job.get("changed_files") or [])
        if mode == "review":
            result["findings"] = dummy_findings(rng, language, files, ctx.head_dir, changed)
        elif mode == "gate":
            result["findings"] = dummy_findings(rng, language, files, ctx.head_dir, changed, limit=3)
            result["measurement"] = dummy_measurement(rng)
        else:
            result["findings"] = dummy_findings(rng, language, files, ctx.head_dir, changed)
            result["candidates"] = dummy_candidates(rng, result["findings"])
            if result["candidates"]:
                best = min(result["candidates"], key=lambda c: c["change"])
                result["measurement"] = dummy_measurement(rng, change=best["change"])

        result["summary"] = summarize(result, files)
        ctx.log("info", f"{result['summary']['findings']} findings")
        return result


def _seed(job: dict) -> int:
    key = f"{job.get('commit_sha') or job['id']}:{job['mode']}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)


def scan_repo(root: Path) -> tuple[str, list[str]]:
    """Language (from joulecode.yml, otherwise by file extensions) and source files."""
    files = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.suffix in EXTENSIONS and not any(part in _SKIP for part in p.parts)
    )
    language = None
    cfg = root / "joulecode.yml"
    if cfg.is_file():
        try:
            language = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("language")
        except yaml.YAMLError:
            language = None
    if not language:
        counts = Counter(EXTENSIONS[Path(f).suffix] for f in files)
        language = counts.most_common(1)[0][0] if counts else "python"
    return str(language), files


_SKIP = {"node_modules", "vendor", ".venv", "venv", "__pycache__", "target", "build", "dist"}


def _functions(root: Path, rel: str, language: str) -> list[tuple[int, str]]:
    pattern = FUNCTION_PATTERNS.get(language)
    if not pattern:
        return []
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for i, line in enumerate(lines, 1):
        m = pattern.match(line)
        if m:
            name = next((g for g in m.groups() if g), None)
            if name and name not in ("if", "for", "while", "switch", "catch", "return", "new"):
                out.append((i, name))
    return out


def _is_test(rel: str) -> bool:
    low = rel.lower()
    return any(t in low for t in ("test", "spec", "bench/", "benchmark"))


def dummy_findings(
    rng: random.Random,
    language: str,
    files: list[str],
    root: Path,
    changed: set[str],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rules = RULES.get(language, RULES["python"])
    lang_ext = {e for e, lang in EXTENSIONS.items() if lang == language}
    sources = [f for f in files if not _is_test(f) and Path(f).suffix in lang_ext] or files
    # Diff-aware: changed files first (the real analysis only considers changed functions).
    if changed:
        sources = [f for f in sources if f in changed] or sources
    candidates = [(f, line, fn) for f in sources for line, fn in _functions(root, f, language)]
    if not candidates:
        return []
    n = min(len(candidates), limit or rng.randint(3, 8))
    picked = sorted(rng.sample(candidates, n))
    findings = []
    for i, (rel, line, fn) in enumerate(picked, 1):
        rule, tool, severity, message = rng.choice(rules)
        evidence = "llm" if tool == "llm" else "static"
        suggestion = None
        if evidence == "llm" or rng.random() < 0.3:
            desc, patch = SUGGESTIONS.get(language, SUGGESTIONS["python"])
            suggestion = {"description": desc, "patch": patch}
        findings.append(
            {
                "id": f"F{i}",
                "rule": rule,
                "tool": "LLM" if tool == "llm" else tool,
                "severity": severity,
                "evidence": evidence,
                "file": rel,
                "line": line + rng.randint(1, 4),
                "function": fn,
                "message": message,
                "suggestion": suggestion,
                "in_diff": rel in changed,
            }
        )
    # A finding with measurement evidence shows what it will look like once the platform
    # has collected measured effects per rule (empirical rule catalogue).
    if findings and rng.random() < 0.6:
        f = findings[0]
        f["evidence"] = "measured"
        f["evidence_detail"] = f"in {rng.randint(4, 20)} measured cases median {-rng.uniform(5, 40):.0f}%"
    return findings


def dummy_measurement(rng: random.Random, change: float | None = None) -> dict[str, Any]:
    if change is None:
        change = rng.choice([rng.uniform(-0.25, -0.03), rng.uniform(-0.015, 0.015), rng.uniform(0.04, 0.18)])
    half = rng.uniform(0.008, 0.025)
    lo, hi = change - half, change + half
    baseline = rng.uniform(80, 400)
    n = rng.randint(5, 10)
    if hi < 0:
        verdict, p = "better", rng.uniform(0.0002, 0.008)
    elif lo > 0:
        verdict, p = "worse", rng.uniform(0.0002, 0.008)
    else:
        verdict, p = "no significant difference", rng.uniform(0.2, 0.9)
    return {
        "backend": "dummy",
        "quantity": "dynamic energy (CPU+DRAM)",
        "unit": "J",
        "baseline": {"median": round(baseline, 2), "n": n},
        "candidate": {"median": round(baseline * (1 + change), 2), "n": n},
        "change": round(change, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "p_value": round(p, 4),
        "verdict": verdict,
    }


def dummy_candidates(rng: random.Random, findings: list[dict]) -> list[dict[str, Any]]:
    hotspots = list(dict.fromkeys(f["function"] for f in findings))[:3]
    out = []
    for h_i, hotspot in enumerate(hotspots, 1):
        for c_i in range(1, rng.randint(2, 3) + 1):
            m = dummy_measurement(rng, change=rng.uniform(-0.6, 0.02))
            out.append(
                {
                    "id": f"h{h_i}c{c_i}",
                    "hotspot": hotspot,
                    "change": m["change"],
                    "ci95": m["ci95"],
                    "verdict": m["verdict"],
                    "maintainability": rng.randint(2, 5),
                    "summary": rng.choice(
                        [
                            "Lookup via a set instead of a list",
                            "Compute the intermediate result once instead of in every iteration",
                            "Loop replaced with a built-in function",
                            "Unnecessary copy of the input avoided",
                        ]
                    ),
                }
            )
    return out


def summarize(result: dict[str, Any], files: list[str]) -> dict[str, Any]:
    findings = result["findings"]
    summary = {
        "files": len(files),
        "findings": len(findings),
        "by_evidence": dict(Counter(f["evidence"] for f in findings)),
        "by_severity": dict(Counter(f["severity"] for f in findings)),
        "language": result["language"],
        "dummy": result.get("dummy", False),
    }
    m = result.get("measurement")
    if m:
        summary["change"] = m["change"]
        summary["ci95"] = m["ci95"]
        summary["verdict"] = m["verdict"]
    if result.get("candidates"):
        summary["candidates"] = len(result["candidates"])
    return summary
