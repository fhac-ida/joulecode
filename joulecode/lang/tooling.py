"""Common basis for language back-ends whose tools do not run in Python (PHP, JavaScript).

Every such back-end comes with a directory containing a helper script (`tool.php`, `tool.js`),
the linter configurations and a Dockerfile. From it, joule:code builds a tool image on first
use (`joulecode/<language>-tools:<hash of the directory>`) so that the
measurement machine needs neither PHP nor Node. For development and tests it also works locally
(`JOULECODE_<LANGUAGE>_TOOLS=local`).

The helper script reads a JSON request from stdin and answers with JSON. All scripts
speak the same schema, which is why the code operations are implemented here once for all:

* `functions {source}` → functions with spans (incl. doc comment), signature, doc text, complexity
* `parse_function {code, method}` → checks that the code is exactly one function/method
* `lint {source}` → syntax check
* `file_info {source[, path]}` → MI, SLOC, imports and insertion point, declared/referenced names
* `analyze {root, files}` → `functions` + `file_info` for many files
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import textwrap
from abc import abstractmethod
from pathlib import Path

from joulecode.analysis import FileMetrics, Finding, FunctionMetrics, RepoMetrics, StaticAnalysis
from joulecode.lang.base import FunctionInfo, Language


class ToolError(RuntimeError):
    pass


def tools_image(tools_dir: Path, name: str) -> str:
    """Image tag from the content of the tool directory – rebuilt after every change."""
    digest = hashlib.sha256()
    for f in sorted(tools_dir.iterdir()):
        if f.is_file():
            digest.update(f.name.encode())
            digest.update(f.read_bytes())
    return f"joulecode/{name}-tools:{digest.hexdigest()[:12]}"


class ToolBox:
    """Calls the helper script and the linters – in the tool image or locally."""

    #: Subclasses set these values.
    tools_dir: Path
    name: str  # "php", "js"
    interpreter: str  # "php", "node"
    script: str  # "tool.php", "tool.js"
    error_cls: type[Exception] = ToolError

    def __init__(self, mode: str | None = None) -> None:
        self.mode = mode or os.environ.get(f"JOULECODE_{self.name.upper()}_TOOLS", "docker")
        self.image = tools_image(self.tools_dir, self.name)
        self._image_ready = False
        self._cache: dict[str, dict] = {}

    # Image ------------------------------------------------------------------------
    def ensure_image(self) -> None:
        if self.mode == "local" or self._image_ready:
            return
        probe = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True)
        if probe.returncode != 0:
            # Own label: `joulecode cleanup` only removes run artefacts (joulecode=1),
            # the tool image is kept.
            print(f"Building {self.image} (one-time) …")
            res = subprocess.run(
                ["docker", "build", "-t", self.image, "--label", "joulecode.tools=1", str(self.tools_dir)],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise self.error_cls(f"Tool image {self.image} could not be built:\n{res.stderr[-3000:]}")
        self._image_ready = True

    def local_argv(self, argv: list[str]) -> list[str]:
        """Local call of a tool (subclasses: e.g. from vendor/bin or node_modules/.bin)."""
        return argv

    def _command(self, argv: list[str], root: Path | None) -> tuple[list[str], str | None]:
        """Command in the image (repository under /repo, read-only) or locally; plus the working directory."""
        if self.mode == "local":
            return self.local_argv(argv), str(root) if root else None
        self.ensure_image()
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--label", "joulecode=1"]
        if root:
            cmd += ["-v", f"{root.resolve()}:/repo:ro", "-w", "/repo"]
        return cmd + [self.image, *argv], None

    def script_argv(self) -> list[str]:
        """Call of the helper script (Java: class instead of file)."""
        return [self.interpreter, self.tool_path(self.script)]

    def tool_path(self, name: str) -> str:
        return str(self.tools_dir / name) if self.mode == "local" else f"/opt/joulecode/{name}"

    def repo_path(self, root: Path) -> str:
        return str(root.resolve()) if self.mode == "local" else "/repo"

    # Helper script -------------------------------------------------------------------
    def call(self, op: str, mount: Path | None = None, **payload) -> dict:
        """Operation of the helper script; `mount` = repository the operation needs file access to."""
        request = json.dumps({"op": op, **payload}, ensure_ascii=False)
        key = hashlib.sha256(request.encode()).hexdigest() if mount is None else None
        if key and key in self._cache:
            return self._cache[key]
        cmd, cwd = self._command(self.script_argv(), mount)
        res = subprocess.run(cmd, input=request, capture_output=True, text=True, cwd=cwd)
        try:
            answer = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            detail = res.stderr[-2000:] or res.stdout[-2000:]
            raise self.error_cls(f"{self.script} ({op}) without a valid answer: {detail}") from exc
        if key:
            self._cache[key] = answer
        return answer

    def run(self, argv: list[str], root: Path, timeout: int = 600) -> subprocess.CompletedProcess:
        cmd, cwd = self._command(argv, root)
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)


# -- Helpers ----------------------------------------------------------------------------
SKIP_DIRS = {".git", "vendor", "node_modules", "var", "cache", "build", "dist", "coverage", ".next"}


def source_files(repo: Path, excluded: list[str], suffixes: tuple[str, ...]) -> list[str]:
    out = []
    for path in sorted(p for p in repo.rglob("*") if p.suffix in suffixes and p.is_file()):
        rel = path.relative_to(repo).as_posix()
        if any(part in SKIP_DIRS or part.startswith(".") for part in Path(rel).parts[:-1]):
            continue
        if any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excluded):
            continue
        out.append(rel)
    return out


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def cc_rank(cc: int) -> str:
    # Ranks as in radon so that reports of all languages read the same.
    for limit, rank in ((5, "A"), (10, "B"), (20, "C"), (30, "D"), (40, "E")):
        if cc <= limit:
            return rank
    return "F"


def format_doc_block(text: str, indent: str) -> str:
    """`/** … */` block (PHPDoc, JSDoc) from plain text."""
    body = text.strip().replace("*/", "* /").splitlines() or [""]
    lines = [f"{indent}/**"] + [f"{indent} *" + (f" {line}" if line.strip() else "") for line in body]
    return "\n".join([*lines, f"{indent} */"]) + "\n"


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def diff_hunks(diff: str) -> list[tuple[int, list[str]]]:
    """Unified diff → (first changed line in the original, changed lines) per hunk."""
    hunks: list[tuple[int, list[str]]] = []
    line_no, first, changes = 0, 0, []
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if m:
            if changes:
                hunks.append((first, changes))
            line_no, first, changes = int(m[1]), 0, []
        elif not line_no or line.startswith(("---", "+++")):
            continue
        elif line.startswith(("-", "+")):
            if line[1:].strip():  # pure blank-line changes are not a finding
                first = first or line_no
                changes.append(line[:120])
            line_no += line.startswith("-")
        else:
            line_no += 1
    if changes:
        hunks.append((first, changes))
    return hunks


def relative(file: str, root_prefix: str) -> str:
    return file[len(root_prefix) :].lstrip("/") if file.startswith(root_prefix) else file


# -- Common part of the back-ends ----------------------------------------------------------
class ToolLanguage(Language):
    """Code operations and analysis via the helper script; subclasses add linters, imports, profiling."""

    method_sep: str  # "::" (PHP) or "." (JavaScript)
    suffixes: tuple[str, ...]
    lint_name: str  # tool name in findings (e.g. "rector", "eslint")
    dead_name: str
    metrics_labels: tuple[str, str, str]  # (language, linter, dead code) for RepoMetrics

    def __init__(self, tools: ToolBox | None = None) -> None:
        self._tools = tools

    @property
    def tools(self) -> ToolBox:
        return self._tools or self.default_tools()

    @abstractmethod
    def default_tools(self) -> ToolBox: ...

    def _call(self, op: str, **payload) -> dict:
        res = self.tools.call(op, **payload)
        if not res.get("ok"):
            raise ValueError(res.get("error", f"{self.tools.script} {op} failed"))
        return res

    # Code operations ---------------------------------------------------------------
    def functions(self, source: str) -> list[FunctionInfo]:
        lines = source.splitlines(keepends=True)
        return [
            FunctionInfo(
                qualname=f["qualname"],
                start=f["start"],
                end=f["end"],
                def_line=f["def_line"],
                indent=indent_of(lines[f["start"] - 1]) if f["start"] <= len(lines) else "",
                signature=f["signature"],
                docstring=f["docstring"],
                complexity=f["complexity"],
            )
            for f in self._call("functions", source=source)["functions"]
        ]

    def _raw(self, source: str, qualname: str) -> dict | None:
        return next(
            (f for f in self._call("functions", source=source)["functions"] if f["qualname"] == qualname),
            None,
        )

    def check_syntax(self, source: str) -> None:
        self._call("lint", source=source)

    def clean_code(self, code: str) -> str:
        """Clean up LLM output before applying it (e.g. remove `<?php`)."""
        return code

    def replace_function(self, source: str, qualname: str, new_code: str) -> str:
        span = self.find_function(source, qualname)
        if span is None:
            raise KeyError(f"Function {qualname!r} not found.")
        method = self.method_sep in qualname
        code = textwrap.dedent(self.clean_code(new_code)).strip("\n") + "\n"
        parsed = self._call("parse_function", code=code, method=method)
        expected = qualname.rsplit(self.method_sep, 1)[-1]
        if parsed["name"].lower() != expected.lower():
            raise ValueError(f"Function is called {parsed['name']!r}, expected {expected!r}.")
        indented = textwrap.indent(code, span.indent, lambda line: line.strip() != "")
        lines = source.splitlines(keepends=True)
        result = "".join(lines[: span.start - 1]) + indented + "".join(lines[span.end :])
        self.check_syntax(result)
        return result

    def insert_docstring(self, source: str, qualname: str, text: str) -> str:
        f = self._raw(source, qualname)
        if f is None:
            raise KeyError(qualname)
        lines = source.splitlines(keepends=True)
        if f["doc_start"]:
            indent = indent_of(lines[f["doc_start"] - 1])
            result = (
                "".join(lines[: f["doc_start"] - 1])
                + format_doc_block(text, indent)
                + "".join(lines[f["doc_end"] :])
            )
        else:
            at = f["node_start"]
            indent = indent_of(lines[at - 1])
            result = "".join(lines[: at - 1]) + format_doc_block(text, indent) + "".join(lines[at - 1 :])
        self.check_syntax(result)
        return result

    def canonical(self, source: str) -> str:
        # token sequence without whitespace and comments; string contents stay unchanged.
        return self._call("canonical", source=source)["canonical"]

    def complexity(self, source: str, qualname: str) -> int | None:
        f = self._raw(source, qualname)
        return f["complexity"] if f else None

    def maintainability_index(self, source: str) -> float:
        return float(self._call("file_info", source=source)["mi"])

    def internal_modules(self, static: StaticAnalysis) -> frozenset[str]:
        return frozenset(static.internal_names)

    def insert_lines(self, source: str, after_line: int, block: str) -> str:
        lines = source.splitlines(keepends=True)
        result = "".join(lines[:after_line]) + block + "".join(lines[after_line:])
        self.check_syntax(result)
        return result

    # Analysis -----------------------------------------------------------------------
    @abstractmethod
    def linters(self, repo: Path, files: list[str]) -> list[tuple[str, list[str], callable]]:
        """(name, command, parser of the output → findings) per linter."""

    def analyze_repo(self, repo: Path, excluded: list[str]) -> StaticAnalysis:
        from joulecode.analysis import fill_metrics

        files = source_files(repo, excluded, self.suffixes)
        lang, lint, dead = self.metrics_labels
        result = StaticAnalysis(metrics=RepoMetrics(language=lang, lint_tool=lint, dead_code_tool=dead))
        if not files:
            return result
        parser_name = self.tools.script
        try:
            answer = self.tools.call("analyze", mount=repo, root=self.tools.repo_path(repo), files=files)
        except (ToolError, OSError) as exc:
            result.tool_errors.append(f"{parser_name}: {exc}")
            return result
        if not answer.get("ok"):
            result.tool_errors.append(f"{parser_name}: {answer.get('error')}")
            return result

        declared: dict[str, str] = {}
        referenced: dict[str, list[str]] = {}
        for rel, data in answer["files"].items():
            if "error" in data:
                result.tool_errors.append(f"{parser_name}: {rel}: {data['error']}")
                continue
            info = data["info"]
            result.files.append(FileMetrics(rel, info["sloc"], info["mi"]))
            result.functions.extend(
                FunctionMetrics(
                    rel, f["qualname"], f["start"], f["end"], f["complexity"], cc_rank(f["complexity"])
                )
                for f in data["functions"]
            )
            for name in info["declared"]:
                declared[name.lower()] = rel
            referenced[rel] = info["referenced"]

        root_prefix = self.tools.repo_path(repo)
        for name, argv, parse in self.linters(repo, files):
            try:
                res = self.tools.run(argv, repo)
                result.findings.extend(parse(res.stdout, root_prefix, res))
            except (ToolError, OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
                result.tool_errors.append(f"{name}: {exc}")

        # coupling: file A depends on file B if A uses something from B.
        imports = {
            rel: {declared[n.lower()] for n in refs if n.lower() in declared and declared[n.lower()] != rel}
            for rel, refs in referenced.items()
        }
        fan_in: dict[str, int] = {}
        for targets in imports.values():
            for t in targets:
                fan_in[t] = fan_in.get(t, 0) + 1
        result.coupling = {
            rel: {"fan_out": len(t), "fan_in": fan_in.get(rel, 0)} for rel, t in imports.items()
        }
        result.internal_names = self.internal_from(answer)
        fill_metrics(result, lint_tool=self.lint_name, dead_tool=self.dead_name)
        return result

    @abstractmethod
    def internal_from(self, answer: dict) -> list[str]:
        """Names that belong to the project (from the analyze answer)."""


def require_json(output: str, opener: str = "{") -> str:
    """The JSON part of a tool output (cut off warnings before it)."""
    if opener not in output:
        raise ToolError(output[-1500:] or "no output")
    return output[output.index(opener) :]


__all__ = [
    "Finding",
    "ToolBox",
    "ToolError",
    "ToolLanguage",
    "cc_rank",
    "diff_hunks",
    "format_doc_block",
    "indent_of",
    "relative",
    "require_json",
    "source_files",
    "tools_image",
]
