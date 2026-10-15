"""Common interface of the language back-ends.

The measurement, test and container parts of joule:code are language-neutral: they only
run the commands from `joulecode.yml`. Four things are language-specific,
and every back-end provides them:

* **Static analysis** – complexity, maintainability index, findings of
  existing linters, dead code, coupling (`analyze_repo`).
* **Profiling** – which functions have the most self time in the benchmark
  (`profile`).
* **Code operations** – finding and replacing functions, inserting imports and
  documentation comments, comparing signatures. The optimiser returns
  whole functions; applying them happens deterministically.
* **Prompts** – wording of the agents (language, conventions, examples).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from joulecode.analysis import StaticAnalysis
    from joulecode.config import ProjectConfig
    from joulecode.container import Runner
    from joulecode.profiling import ProfileResult


@dataclass
class FunctionInfo:
    """A function or method in the source code, described language-neutrally."""

    qualname: str  # Python: "Class.method", PHP: "Class::method"
    start: int  # 1-based, incl. decorators/attributes/doc comment before it
    end: int  # 1-based, inclusive
    def_line: int  # line with `def` or `function`
    indent: str
    signature: str  # comparable representation; unequal = API break
    docstring: str | None
    complexity: int | None = None

    def source(self, lines: list[str]) -> str:
        return "".join(lines[self.start - 1 : self.end])


@dataclass(frozen=True)
class LanguagePrompts:
    """Language-specific building blocks of the agent prompts."""

    language: str  # "Python", "PHP"
    fence: str  # language of the Markdown code block
    stdlib: str  # what counts as an allowed dependency
    idioms: str  # additional rules for the optimiser
    qualname_example: str  # e.g. "`Class.method`"
    import_example: str  # e.g. "`from collections import Counter`"
    doc_term: str  # "docstring", "PHPDoc block"
    doc_format: str  # what format the documenter should return
    function_hint: str  # how a method should be returned
    lint_tool: str  # linter whose findings the optimiser receives


class Language(ABC):
    name: str
    display: str
    prompts: LanguagePrompts
    profiler: str  # for reports: "cProfile", "Xdebug"

    # -- Analysis and profiling -----------------------------------------------
    @abstractmethod
    def analyze_repo(self, repo: Path, excluded: list[str]) -> StaticAnalysis: ...

    @abstractmethod
    def profile(
        self, cfg: ProjectConfig, runner: Runner, image: str, repo: Path, *, log=print
    ) -> ProfileResult: ...

    @abstractmethod
    def internal_modules(self, static: StaticAnalysis) -> frozenset[str]:
        """Names that belong to the project (not a new dependency if a candidate uses them)."""

    # -- Code operations ------------------------------------------------------
    @abstractmethod
    def functions(self, source: str) -> list[FunctionInfo]: ...

    def find_function(self, source: str, qualname: str) -> FunctionInfo | None:
        return next((f for f in self.functions(source) if f.qualname == qualname), None)

    def canonical(self, source: str) -> str:
        """Comparison form of the source code without formatting and comments (for duplicate detection).

        Two candidates that differ only in line breaks, indentation (where it has no meaning)
        or comments are the same program and are measured only once.
        """
        import re

        return re.sub(r"\s+", " ", source).strip()

    def resolve_name(self, source: str, name: str) -> str:
        """Qualified name for an LLM answer without a class (`parse` instead of `Parser.parse`).

        Small models often omit the class. If exactly one function of the file matches
        the short name, its name is used; otherwise the answer stays unchanged.
        """
        short = name.replace("::", ".").rsplit(".", 1)[-1].lower()
        names = [f.qualname for f in self.functions(source)]
        if name in names:
            return name
        matches = [q for q in names if q.replace("::", ".").rsplit(".", 1)[-1].lower() == short]
        return matches[0] if len(matches) == 1 else name

    @abstractmethod
    def replace_function(self, source: str, qualname: str, new_code: str) -> str:
        """Replace a function with `new_code` (without indentation of the surrounding class)."""

    @abstractmethod
    def normalize_imports(self, imports: list[str], code: str) -> list[str]: ...

    @abstractmethod
    def add_imports(self, source: str, imports: list[str]) -> str: ...

    @abstractmethod
    def insert_docstring(self, source: str, qualname: str, text: str) -> str: ...

    @abstractmethod
    def complexity(self, source: str, qualname: str) -> int | None:
        """Cyclomatic complexity of a function in the source code."""

    @abstractmethod
    def maintainability_index(self, source: str) -> float: ...

    @abstractmethod
    def new_dependencies(
        self, before: str, after: str, internal: frozenset[str]
    ) -> tuple[list[str], list[str]]:
        """(newly imported names, of which dependencies outside the project and the standard library)."""
