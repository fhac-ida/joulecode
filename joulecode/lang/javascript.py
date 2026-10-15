"""JavaScript/TypeScript back-end (Node.js): TypeScript compiler API, ESLint, V8 CPU profiler.

The tools run in the image `joulecode/js-tools` (Dockerfile in `js_tools/`), which
joule:code builds on first use – the measurement machine does not need Node. Locally (development,
tests): `JOULECODE_JS_TOOLS=local` and `JOULECODE_JS_NODE_MODULES=<path/to/node_modules>`
with typescript, eslint, typescript-eslint, eslint-plugin-unicorn and globals.

* **Code operations:** `tool.js` (TypeScript compiler API, understands JS and TS) returns
  functions, class methods and `const f = (…) => …` with spans incl. JSDoc, signature,
  complexity, imports and modules. Qualified names: `Class.method` or `function`.
* **Analysis:** complexity as ESLint `complexity`, maintainability index with the same formula
  as for Python/PHP, ESLint with a small selection of efficiency rules (core + unicorn) as
  findings, `no-unused-vars` as dead code, coupling via relative imports.
* **New dependency:** a package that neither belongs to Node (`node:fs` …) nor is listed in a
  `package.json` of the repository. `package.json`/lock files belong to the protected paths.
* **Profiling:** `node --cpu-prof` via `NODE_OPTIONS` – no separate image, no rewriting
  of the benchmark command (also works for `npm run …`). The profiler samples (200 µs); time in
  built-in functions counts towards the caller. The analysis happens in the container with `cpuprofile.js`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from joulecode.analysis import Finding, StaticAnalysis
from joulecode.lang.base import LanguagePrompts
from joulecode.lang.tooling import ToolBox, ToolError, ToolLanguage, relative, require_json
from joulecode.lang.tooling import tools_image as _tools_image
from joulecode.profiling import Hotspot, ProfileResult, _parse_stats

TOOLS_DIR = Path(__file__).with_name("js_tools")
SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
PROFILE_DIR = "/tmp/joulecode-prof"
# Finer than the default (1 ms) so that short profiling runs also get enough samples.
PROFILE_INTERVAL_US = 200
DEAD_RULES = {"no-unused-vars", "@typescript-eslint/no-unused-vars"}

PROMPTS = LanguagePrompts(
    language="JavaScript/TypeScript",
    fence="ts",
    stdlib="the JavaScript language and the Node.js modules (`node:…`) as well as the packages already listed in package.json",
    idioms=(
        "- Readable, idiomatic code instead of tricks; keep JSDoc and adapt it if necessary; leave the TypeScript types "
        "of the signature unchanged.\n"
        "- Set/Map instead of includes/indexOf/find in loops; no spread copies in reduce or loops "
        "(O(n²)); do not create arrays/objects anew in hot loops; join instead of repeated "
        "string concatenation for many parts.\n"
        "- Mind JavaScript semantics: object keys are strings and integer keys are "
        "sorted numerically first ({'10':…, '9':…} → '9' first) – use Map for order and types; "
        "Array.prototype.sort without a compare function sorts as strings; === instead of ==; async/await "
        "order and error behaviour stay the same."
    ),
    qualname_example="`Class.method` or `functionName`",
    import_example="`import { readFile } from 'node:fs/promises';`",
    doc_term="JSDoc comment",
    doc_format="only the text, without /** */ and without leading asterisks",
    function_hint=(
        "Return every changed function or method COMPLETELY (incl. JSDoc, `export`, `async`, "
        "`static`, visibility and types), without the surrounding class and without its indentation. A function "
        "defined as `const f = (…) => …` stays in this form."
    ),
    lint_tool="ESLint",
)


class JsToolError(ToolError):
    pass


def tools_image() -> str:
    return _tools_image(TOOLS_DIR, "js")


class JsTools(ToolBox):
    """tool.js and ESLint – in the image joulecode/js-tools or locally."""

    tools_dir = TOOLS_DIR
    name = "js"
    interpreter = "node"
    script = "tool.js"
    error_cls = JsToolError

    def local_argv(self, argv: list[str]) -> list[str]:
        modules = os.environ.get("JOULECODE_JS_NODE_MODULES")
        local_bin = Path(modules) / ".bin" / argv[0] if modules else None
        if local_bin and local_bin.is_file():
            return [str(local_bin), *argv[1:]]
        return argv

    def tool_path(self, name: str) -> str:
        modules = os.environ.get("JOULECODE_JS_NODE_MODULES")
        if self.mode == "local" and name.endswith(".mjs") and modules:
            # The ESLint configuration loads its plugins relative to its location.
            target = Path(modules).parent / name
            shutil.copyfile(TOOLS_DIR / name, target)
            return str(target)
        return super().tool_path(name)


_TOOLS: JsTools | None = None


def default_tools() -> JsTools:
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = JsTools()
    return _TOOLS


def parse_eslint(output: str, root_prefix: str, _res=None) -> list[Finding]:
    """ESLint JSON → findings; `no-unused-vars` counts as dead code."""
    data = json.loads(require_json(output, "["))
    findings = []
    for f in data:
        file = relative(f["filePath"], root_prefix)
        for m in f.get("messages", []):
            rule = m.get("ruleId") or ""
            if not rule and m.get("fatal"):
                continue  # tool.js reports parser errors more precisely
            tool = "eslint-dead" if rule in DEAD_RULES else "eslint"
            findings.append(Finding(tool, rule, m.get("message", "").strip(), file, m.get("line") or 0))
    return findings


def package_names(repo: Path) -> list[str]:
    """Names and dependencies of all package.json files in the repository (without node_modules) – not a new dependency."""
    names: set[str] = set()
    for pj in repo.rglob("package.json"):
        if "node_modules" in pj.parts:
            continue
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("name"):
            names.add(data["name"])
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            names.update((data.get(key) or {}).keys())
    return sorted(names)


def map_v8_name(function: str) -> str:
    """V8 usually names methods only by their name; `Class.method` occurs with newer versions."""
    return function.rsplit(" ", 1)[-1]  # "get foo" → "foo"


class JavaScriptLanguage(ToolLanguage):
    name = "javascript"
    display = "JavaScript/TypeScript"
    prompts = PROMPTS
    profiler = "V8 --cpu-prof"
    method_sep = "."
    suffixes = SUFFIXES
    lint_name, dead_name = "eslint", "eslint-dead"
    metrics_labels = ("JavaScript/TypeScript", "ESLint", "ESLint no-unused-vars")

    def __init__(self, tools: JsTools | None = None, *, typescript: bool = False) -> None:
        super().__init__(tools)
        # No TypeScript code blocks for plain JavaScript – otherwise the LLM likes to add types.
        self.prompts = (
            dataclasses.replace(PROMPTS, language="TypeScript", fence="ts")
            if typescript
            else dataclasses.replace(PROMPTS, language="JavaScript", fence="js")
        )

    def default_tools(self) -> JsTools:
        return default_tools()

    def clean_code(self, code: str) -> str:
        return re.sub(r"^\s*['\"]use strict['\"];?\s*\n", "", code)

    # Imports -------------------------------------------------------------------------
    def normalize_imports(self, imports: list[str], code: str) -> list[str]:
        out = []
        for raw in imports:
            stmt = raw.strip()
            if not stmt:
                continue
            out.append(stmt if stmt.endswith(";") else stmt + ";")
        return out

    def add_imports(self, source: str, imports: list[str]) -> str:
        if not imports:
            return source
        info = self._call("file_info", source=source)
        existing = {re.sub(r"\s+", " ", u) for u in info["uses"]}
        new = []
        for stmt in imports:
            parsed = self._call("file_info", source=stmt)
            if len(parsed["uses"]) != 1:
                raise ValueError(f"Not a single import/require statement: {stmt!r}")
            norm = parsed["uses"][0]
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
        third_party = sorted(m for m in new if not m.startswith("node:") and m not in internal)
        return new, third_party

    # Analysis -----------------------------------------------------------------------
    def internal_from(self, answer: dict) -> list[str]:
        return []  # analyze_repo adds them from the package.json files

    def analyze_repo(self, repo: Path, excluded: list[str]) -> StaticAnalysis:
        result = super().analyze_repo(repo, excluded)
        result.internal_names = package_names(repo)
        return result

    def linters(self, repo: Path, files: list[str]):
        eslint = [
            "eslint", "--no-config-lookup", "--config", self.tools.tool_path("eslint.config.mjs"),
            "--format", "json", "--no-error-on-unmatched-pattern", *files,
        ]  # fmt: skip
        return [("eslint", eslint, parse_eslint)]

    # Profiling ----------------------------------------------------------------------
    def profile(self, cfg, runner, image, repo, *, log=print) -> ProfileResult:
        prof_opts = f"--cpu-prof --cpu-prof-dir={PROFILE_DIR} --cpu-prof-interval={PROFILE_INTERVAL_US}"
        env = {**cfg.benchmark.env, **cfg.profile.env}
        env["NODE_OPTIONS"] = f"{env.get('NODE_OPTIONS', '')} {prof_opts}".strip()
        command = cfg.profile.command or cfg.benchmark.command
        script = (TOOLS_DIR / "cpuprofile.js").read_text(encoding="utf-8")
        box = runner.start(image, cfg.limits)
        try:
            log("Profiling run (V8 --cpu-prof) …")
            res = box.exec(f"mkdir -p {PROFILE_DIR} && {command}", cfg.profile.timeout_s, env)
            if not res.ok:
                return ProfileResult(
                    ok=False,
                    profiler=self.profiler,
                    error=f"Profiling run exit {res.exit_code}:\n{res.output[-2000:]}",
                )
            stats_cmd = (
                f"node -e {shlex.quote(script)} {PROFILE_DIR} {shlex.quote(json.dumps(cfg.protected_paths))}"
            )
            # Do not profile the analysis itself.
            stats_res = box.exec(stats_cmd, 300, {"NODE_OPTIONS": ""})
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
                error="The CPU profile contains no functions from the repository. Does the benchmark run with node "
                "(NODE_OPTIONS is picked up) and is the code located under the image's working directory?",
            )
        hotspots = self.select_hotspots(
            stats, repo, limit=cfg.optimize.hotspots, min_share=cfg.optimize.min_share
        )
        return ProfileResult(ok=True, profiler=self.profiler, total_s=stats["total_s"], hotspots=hotspots)

    def select_hotspots(self, stats: dict, repo: Path, *, limit: int, min_share: float) -> list[Hotspot]:
        total = stats["total_s"] or 1e-9
        hotspots: list[Hotspot] = []
        # The same function can occur several times (e.g. several processes) – merge them.
        merged: dict[tuple[str, str, int], dict] = {}
        for row in stats["functions"]:
            key = (row["file"], row["function"], row["line"])
            m = merged.setdefault(key, {**row, "tottime": 0.0, "cumtime": 0.0})
            m["tottime"] += row["tottime"]
            m["cumtime"] += row["cumtime"]
        for row in sorted(merged.values(), key=lambda r: r["tottime"], reverse=True):
            share = row["tottime"] / total
            if share < min_share or len(hotspots) >= limit:
                break
            path = repo / row["file"]
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            try:
                funcs = self.functions(source)
            except ValueError:
                continue
            short = map_v8_name(row["function"]).rsplit(".", 1)[-1]
            by_name = [f for f in funcs if f.qualname.rsplit(".", 1)[-1] == short]
            by_line = [f for f in funcs if f.start <= row["line"] <= f.end]
            span = (
                by_name[0]
                if len(by_name) == 1
                else next((f for f in by_name if f in by_line), None) or (by_line[0] if by_line else None)
            )
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
