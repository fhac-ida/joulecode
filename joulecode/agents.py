"""The three agents: optimiser, maintainability reviewer, documenter.

Every agent is a single, clearly delimited LLM call with a JSON schema.
Decisions that can be measured are made by deterministic rules:
syntax, signature, new dependencies, increase in complexity. The reviewer's
LLM is the second voice for what cannot be measured.
"""

from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from joulecode import prompts
from joulecode.analysis import StaticAnalysis
from joulecode.lang import Language, get_language
from joulecode.llm import LLM, LLMError
from joulecode.profiling import Hotspot
from joulecode.variants import IGNORE

MAX_CONTEXT_LINES = 400


@dataclass
class Candidate:
    id: str
    file: str
    hotspot: str
    title: str
    rationale: str
    imports: list[str]
    functions: list[dict]  # [{"name": qualname, "code": str}]
    round: int = 0
    origin: str = "initial"  # initial | repair | revision
    status: str = "new"  # new | invalid | tests_failed | vetoed | ready | measured
    messages: list[str] = field(default_factory=list)
    path: Path | None = None
    patch: str = ""
    tests_output: str = ""
    review: dict = field(default_factory=dict)
    docs: list[str] = field(default_factory=list)
    # For combinations: the IDs of the combined candidates.
    parts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return [f["name"] for f in self.functions]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path) if self.path else None
        return d


# -- Optimiser -----------------------------------------------------------------
class Optimizer:
    def __init__(self, llm: LLM, log=print, lang: Language | None = None) -> None:
        self.llm = llm
        self.log = log
        self.lang = lang or get_language()

    def propose(
        self,
        hotspot: Hotspot,
        baseline: Path,
        static: StaticAnalysis,
        n: int,
        feedback: str = "",
    ) -> list[dict]:
        source = (baseline / hotspot.file).read_text(encoding="utf-8")
        context = source if source.count("\n") <= MAX_CONTEXT_LINES else hotspot.code
        fm = static.function(hotspot.file, hotspot.qualname)
        hints = static.findings_in(hotspot.file, hotspot.line, hotspot.end_line)
        prompt = prompts.render(
            prompts.OPTIMIZER_PROMPT,
            self.lang.prompts,
            qualname=hotspot.qualname,
            file=hotspot.file,
            share=hotspot.share,
            calls=f"{hotspot.ncalls} calls" if hotspot.ncalls else "call count unknown (sampling profiler)",
            tottime=hotspot.tottime,
            cumtime=hotspot.cumtime,
            complexity=fm.complexity if fm else "unknown",
            hints="\n".join(f"- {h.code}: {h.message} (line {h.line})" for h in hints) or "- none",
            code=hotspot.code,
            context=context,
            feedback=feedback,
            n=n,
        )
        try:
            system = prompts.render(prompts.OPTIMIZER_SYSTEM, self.lang.prompts)
            answer = self.llm.generate_json(system, prompt, prompts.OPTIMIZER_SCHEMA)
        except LLMError as exc:
            self.log(f"Optimiser: {exc}")
            return []
        usable = [c for c in answer.get("candidates", []) if c.get("functions")][:n]
        if not usable:
            self.log(
                f"Optimiser: answer without usable candidates: {json.dumps(answer, ensure_ascii=False)[:300]}"
            )
        return usable


def _apply(candidate: Candidate, target: Path, lang: Language) -> None:
    path = target / candidate.file
    source = path.read_text(encoding="utf-8")
    kept = []
    for fn in candidate.functions:
        fn["name"] = lang.resolve_name(source, fn["name"])
        updated = lang.replace_function(source, fn["name"], fn["code"])
        if " ".join(updated.split()) == " ".join(source.split()):
            continue  # returned unchanged (often the hotspot itself) – no change
        source = updated
        kept.append(fn)
    if kept and len(kept) < len(candidate.functions):
        candidate.functions = kept
    code = "\n".join(fn["code"] for fn in candidate.functions)
    candidate.imports = lang.normalize_imports(candidate.imports, code)
    source = lang.add_imports(source, candidate.imports)
    path.write_text(source, encoding="utf-8")


def _fresh_copy(baseline: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(baseline, target, ignore=IGNORE, symlinks=True)


def materialize(candidate: Candidate, baseline: Path, target: Path, lang: Language | None = None) -> None:
    """Copy the original to `target` and apply the candidate's functions."""
    _fresh_copy(baseline, target)
    _apply(candidate, target, lang or get_language())
    candidate.path = target
    candidate.patch = make_patch(baseline, target, [candidate.file])


def combine(
    parts: list[Candidate], baseline: Path, target: Path, cid: str = "combined", lang: Language | None = None
) -> Candidate:
    """Apply several candidates (of different hotspots) together."""
    names = [(p.file, n) for p in parts for n in p.changed]
    if len(names) != len(set(names)):
        raise ValueError("Candidates change the same function and cannot be combined.")
    _fresh_copy(baseline, target)
    for p in parts:
        _apply(p, target, lang or get_language())
    files = sorted({p.file for p in parts})
    combo = Candidate(
        id=cid,
        file=", ".join(files),
        hotspot=" + ".join(p.hotspot for p in parts),
        title="Combination: " + " + ".join(p.title for p in parts),
        rationale="\n\n".join(f"**{p.id} ({p.hotspot}):** {p.rationale}" for p in parts),
        imports=[],
        functions=[dict(f, file=p.file) for p in parts for f in p.functions],
        origin="combination",
        parts=[p.id for p in parts],
        path=target,
    )
    combo.patch = make_patch(baseline, target, files)
    return combo


def make_patch(baseline: Path, candidate: Path, files: list[str]) -> str:
    out = []
    for rel in files:
        a = (baseline / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        b = (candidate / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        out.extend(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}"))
    return "".join(out)


# -- Maintainability reviewer -----------------------------------------------------
@dataclass
class ReviewSettings:
    max_complexity_increase: int = 3
    internal_modules: frozenset[str] = frozenset()


class Reviewer:
    def __init__(
        self, llm: LLM | None, settings: ReviewSettings, log=print, lang: Language | None = None
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.log = log
        self.lang = lang or get_language()

    def metrics(self, candidate: Candidate, baseline: Path) -> dict:
        before_src = (baseline / candidate.file).read_text(encoding="utf-8")
        after_src = (candidate.path / candidate.file).read_text(encoding="utf-8")
        lang = self.lang
        functions = []
        for name in candidate.changed:
            b, a = lang.find_function(before_src, name), lang.find_function(after_src, name)
            functions.append(
                {
                    "name": name,
                    "complexity_before": lang.complexity(before_src, name) if b else None,
                    "complexity_after": lang.complexity(after_src, name) if a else None,
                    "signature_changed": bool(b and a and b.signature != a.signature),
                    "docstring_before": bool(b and b.docstring),
                    "docstring_after": bool(a and a.docstring),
                }
            )
        new_modules, third_party = lang.new_dependencies(
            before_src, after_src, self.settings.internal_modules
        )
        diff_lines = sum(
            1
            for line in candidate.patch.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        )
        return {
            "functions": functions,
            "mi_before": lang.maintainability_index(before_src),
            "mi_after": lang.maintainability_index(after_src),
            "diff_lines": diff_lines,
            "new_imports": new_modules,
            "new_third_party": third_party,
        }

    def rule_vetoes(self, metrics: dict) -> list[str]:
        vetoes = []
        for fn in metrics["functions"]:
            if fn["signature_changed"]:
                vetoes.append(f"Signature of {fn['name']} changed (API break).")
            cb, ca = fn["complexity_before"], fn["complexity_after"]
            if cb is not None and ca is not None and ca - cb > self.settings.max_complexity_increase:
                vetoes.append(f"Complexity of {fn['name']} rises from {cb} to {ca}.")
        if metrics["new_third_party"]:
            vetoes.append(f"New dependency(ies): {', '.join(metrics['new_third_party'])}.")
        return vetoes

    def review(self, candidate: Candidate, baseline: Path) -> dict:
        metrics = self.metrics(candidate, baseline)
        vetoes = self.rule_vetoes(metrics)
        result = {"metrics": metrics, "rule_vetoes": vetoes, "llm": None, "veto": bool(vetoes)}
        if self.llm is not None and not vetoes:
            before_src = (baseline / candidate.file).read_text(encoding="utf-8")
            after_src = (candidate.path / candidate.file).read_text(encoding="utf-8")

            def code_of(src: str) -> str:
                lines = src.splitlines(keepends=True)
                spans = [self.lang.find_function(src, n) for n in candidate.changed]
                return "\n".join(s.source(lines) for s in spans if s)

            lp = self.lang.prompts
            system = prompts.render(prompts.REVIEWER_SYSTEM, lp)
            prompt = prompts.render(
                prompts.REVIEWER_PROMPT,
                lp,
                file=candidate.file,
                title=candidate.title,
                rationale=candidate.rationale,
                before=code_of(before_src),
                after=code_of(after_src),
                metrics=json.dumps(metrics, ensure_ascii=False),
            )
            try:
                try:
                    llm = self.llm.generate_json(system, prompt, prompts.REVIEWER_SCHEMA, max_tokens=800)
                except LLMError:
                    # Usually a rambling answer cut off at the length limit – try once more, more briefly.
                    llm = self.llm.generate_json(
                        system,
                        prompt + prompts.REVIEWER_RETRY,
                        prompts.REVIEWER_SCHEMA,
                        max_tokens=400,
                    )
                score = int(llm.get("maintainability", 3))
                llm["maintainability"] = min(max(score, 1), 5)
                result["llm"] = llm
                # An LLM veto only counts together with a bad score –
                # otherwise small models reject too quickly.
                result["veto"] = bool(llm.get("veto")) and llm["maintainability"] <= 2
            except LLMError as exc:
                self.log(f"Reviewer: {exc}")
        result["score"] = self.score(result)
        return result

    @staticmethod
    def score(review: dict) -> float:
        """Maintainability score 1–5: LLM judgement, otherwise derived from metrics."""
        if review.get("llm"):
            return float(review["llm"]["maintainability"])
        m = review["metrics"]
        delta_cc = sum((f["complexity_after"] or 0) - (f["complexity_before"] or 0) for f in m["functions"])
        delta_mi = m["mi_after"] - m["mi_before"]
        return round(min(max(3 - 0.5 * delta_cc + delta_mi / 10, 1), 5), 2)


# -- Documenter -------------------------------------------------------------------
class Documenter:
    """Make sure every changed function is documented."""

    def __init__(self, llm: LLM | None, log=print, lang: Language | None = None) -> None:
        self.llm = llm
        self.log = log
        self.lang = lang or get_language()

    def ensure_docstrings(self, candidate: Candidate, baseline: Path) -> list[str]:
        path = candidate.path / candidate.file
        before_src = (baseline / candidate.file).read_text(encoding="utf-8")
        source = path.read_text(encoding="utf-8")
        lang = self.lang
        term = lang.prompts.doc_term
        actions = []
        for name in candidate.changed:
            after = lang.find_function(source, name)
            if after is None or after.docstring:
                continue
            before = lang.find_function(before_src, name)
            if before and before.docstring:
                source = lang.insert_docstring(source, name, before.docstring)
                actions.append(f"{name}: {term} of the original restored.")
            elif self.llm is not None:
                lines = source.splitlines(keepends=True)
                style = next((f.docstring for f in lang.functions(source) if f.docstring), "(none)")
                try:
                    answer = self.llm.generate_json(
                        prompts.render(prompts.DOC_SYSTEM, lang.prompts),
                        prompts.render(
                            prompts.DOC_PROMPT,
                            lang.prompts,
                            file=candidate.file,
                            code=after.source(lines),
                            style=style,
                        ),
                        prompts.DOC_SCHEMA,
                        max_tokens=400,
                    )
                    text = answer.get("docstring", "").strip()
                    if text:
                        source = lang.insert_docstring(source, name, text)
                        actions.append(f"{name}: {term} added (LLM).")
                except LLMError as exc:
                    self.log(f"Documenter: {exc}")
            else:
                actions.append(f"{name}: no {term} present.")
        if source != path.read_text(encoding="utf-8"):
            path.write_text(source, encoding="utf-8")
            candidate.patch = make_patch(baseline, candidate.path, [candidate.file])
            # Update the applied function in the candidate so that the report is correct.
            for fn in candidate.functions:
                span = lang.find_function(source, fn["name"])
                if span:
                    fn["code"] = span.source(source.splitlines(keepends=True))
        return actions
